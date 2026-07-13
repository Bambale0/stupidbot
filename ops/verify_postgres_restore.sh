#!/usr/bin/env bash
set -Eeuo pipefail

original_database_url=${1:?original DATABASE_URL is required}
maintenance_database_url=${2:?maintenance PostgreSQL URL is required}
dump_file=${3:?dump file is required}
candidate_dir=${4:?candidate directory is required}

command -v createdb >/dev/null
command -v dropdb >/dev/null
command -v pg_restore >/dev/null
[[ -s "${dump_file}" ]] || { echo "Restore verification dump is missing" >&2; exit 1; }
[[ -d "${candidate_dir}" ]] || { echo "Restore verification candidate is missing" >&2; exit 1; }

restore_db_name="stupidbot_restore_$(date -u +%Y%m%d_%H%M%S)_${RANDOM}"
restore_created=0

cleanup() {
  status=$?
  if (( restore_created == 1 )); then
    dropdb --if-exists --maintenance-db="${maintenance_database_url}" "${restore_db_name}" \
      >/dev/null 2>&1 || true
  fi
  exit "${status}"
}
trap cleanup EXIT

mapfile -t restore_urls < <(
  ORIGINAL_DATABASE_URL="${original_database_url}" \
  RESTORE_DB_NAME="${restore_db_name}" \
  python3 - <<'PY'
import os
from urllib.parse import urlsplit, urlunsplit

original = os.environ["ORIGINAL_DATABASE_URL"]
name = os.environ["RESTORE_DB_NAME"]
parts = urlsplit(original)
async_url = urlunsplit((parts.scheme, parts.netloc, f"/{name}", parts.query, parts.fragment))
sync_scheme = parts.scheme
for suffix in ("+asyncpg", "+psycopg", "+psycopg2"):
    sync_scheme = sync_scheme.replace(suffix, "")
sync_url = urlunsplit((sync_scheme, parts.netloc, f"/{name}", parts.query, parts.fragment))
print(async_url)
print(sync_url)
PY
)
restore_async_url=${restore_urls[0]}
restore_sync_url=${restore_urls[1]}

printf 'Creating isolated restore database %s\n' "${restore_db_name}"
createdb --maintenance-db="${maintenance_database_url}" "${restore_db_name}"
restore_created=1

pg_restore --exit-on-error --no-owner --no-privileges \
  --dbname="${restore_sync_url}" "${dump_file}"

(
  cd "${candidate_dir}"
  DATABASE_URL="${restore_async_url}" python3 -m scripts.migrate_db
  DATABASE_URL="${restore_async_url}" python3 scripts/regression_financial.py
)

printf 'PostgreSQL restore verification passed for %s\n' "${restore_db_name}"
dropdb --maintenance-db="${maintenance_database_url}" "${restore_db_name}"
restore_created=0
trap - EXIT
