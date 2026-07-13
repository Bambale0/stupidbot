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
restore_create_mode="app"

mapfile -t connection_parts < <(
  ORIGINAL_DATABASE_URL="${original_database_url}" \
  RESTORE_DB_NAME="${restore_db_name}" \
  python3 - <<'PY'
import os
from urllib.parse import unquote, urlsplit, urlunsplit

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
print(unquote(parts.username or ""))
print(parts.hostname or "")
PY
)
restore_async_url=${connection_parts[0]}
restore_sync_url=${connection_parts[1]}
restore_owner=${connection_parts[2]}
database_host=${connection_parts[3]}

run_as_postgres() {
  if [[ $(id -u) -eq 0 ]]; then
    if command -v runuser >/dev/null; then
      runuser -u postgres -- "$@"
      return
    fi
    echo "runuser is required for local PostgreSQL superuser fallback" >&2
    return 1
  fi
  if command -v sudo >/dev/null && sudo -n -u postgres true >/dev/null 2>&1; then
    sudo -n -u postgres -- "$@"
    return
  fi
  echo "Passwordless access to local PostgreSQL OS user is unavailable" >&2
  return 1
}

drop_restore_database() {
  if [[ "${restore_create_mode}" == "postgres" ]]; then
    run_as_postgres dropdb --if-exists "${restore_db_name}"
  else
    dropdb --if-exists --maintenance-db="${maintenance_database_url}" "${restore_db_name}"
  fi
}

cleanup() {
  status=$?
  if (( restore_created == 1 )); then
    drop_restore_database >/dev/null 2>&1 || true
  fi
  exit "${status}"
}
trap cleanup EXIT

printf 'Creating isolated restore database %s\n' "${restore_db_name}"
if createdb --maintenance-db="${maintenance_database_url}" "${restore_db_name}" 2>/tmp/stupidbot-createdb-error.log; then
  restore_create_mode="app"
else
  cat /tmp/stupidbot-createdb-error.log >&2
  case "${database_host}" in
    ""|localhost|127.0.0.1|::1) ;;
    *)
      echo "Application role cannot create databases and PostgreSQL is not local; configure a dedicated restore database" >&2
      exit 1
      ;;
  esac
  [[ -n "${restore_owner}" ]] || {
    echo "Cannot determine application PostgreSQL role from DATABASE_URL" >&2
    exit 1
  }
  echo "Application role lacks CREATEDB; using local PostgreSQL owner fallback"
  run_as_postgres createdb --owner="${restore_owner}" "${restore_db_name}"
  restore_create_mode="postgres"
fi
restore_created=1
rm -f /tmp/stupidbot-createdb-error.log

pg_restore --exit-on-error --no-owner --no-privileges \
  --dbname="${restore_sync_url}" "${dump_file}"

(
  cd "${candidate_dir}"
  DATABASE_URL="${restore_async_url}" python3 -m scripts.migrate_db
  DATABASE_URL="${restore_async_url}" python3 scripts/regression_financial.py
)

printf 'PostgreSQL restore verification passed for %s\n' "${restore_db_name}"
drop_restore_database
restore_created=0
trap - EXIT
