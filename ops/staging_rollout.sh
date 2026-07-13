#!/usr/bin/env bash
set -Eeuo pipefail

app_dir=${1:-/root/stupidbot}
release_sha=${2:?release SHA is required}
service_name=${STUPIDBOT_SERVICE_NAME:-stupidbot}
release_root="${app_dir}/.release"
archive="${release_root}/stupidbot-source.tar.gz"
checksum="${archive}.sha256"
candidate="${release_root}/candidate-${release_sha}"
timestamp=$(date -u +%Y%m%dT%H%M%SZ)
backup_dir="${app_dir}/backups/${timestamp}"
code_backup="${backup_dir}/app-code.tar.gz"
db_backup="${backup_dir}/postgres.dump"
restore_root="${release_root}/rollback-${release_sha}"
mutation_started=0
rollout_succeeded=0

run_root() {
  if [[ $(id -u) -eq 0 ]]; then
    "$@"
  else
    sudo -n "$@"
  fi
}

restart_service() {
  run_root systemctl restart "${service_name}"
  run_root systemctl is-active --quiet "${service_name}"
}

rollback_code() {
  if [[ ! -s "${code_backup}" ]]; then
    echo "Rollback skipped: code backup is unavailable" >&2
    return 0
  fi
  echo "Restoring previous application files"
  rm -rf "${restore_root}"
  mkdir -p "${restore_root}"
  tar -xzf "${code_backup}" -C "${restore_root}"
  rsync --archive --delete \
    --exclude='.env' \
    --exclude='.git/' \
    --exclude='.venv/' \
    --exclude='backups/' \
    --exclude='.release/' \
    "${restore_root}/" "${app_dir}/"
  restart_service || true
}

on_exit() {
  status=$?
  if (( status != 0 )) && (( mutation_started == 1 )) && (( rollout_succeeded == 0 )); then
    echo "Rollout failed; restoring previous code. Database backup remains at ${db_backup}" >&2
    rollback_code || true
  fi
  exit "${status}"
}
trap on_exit EXIT

[[ -d "${app_dir}" ]] || { echo "Application directory does not exist: ${app_dir}" >&2; exit 1; }
[[ -f "${app_dir}/.env" ]] || { echo "Missing ${app_dir}/.env" >&2; exit 1; }
[[ -s "${archive}" ]] || { echo "Missing candidate archive" >&2; exit 1; }
[[ -s "${checksum}" ]] || { echo "Missing candidate checksum" >&2; exit 1; }
command -v pg_dump >/dev/null
command -v pg_restore >/dev/null
command -v createdb >/dev/null
command -v dropdb >/dev/null
command -v rsync >/dev/null
command -v curl >/dev/null

umask 077
mkdir -p "${backup_dir}" "${release_root}"

cd "${release_root}"
sha256sum --check "$(basename "${checksum}")"

set -a
# shellcheck disable=SC1090
source "${app_dir}/.env"
set +a

required_runtime_env=(
  TELEGRAM_BOT_TOKEN
  TELEGRAM_SECRET_TOKEN
  COMET_CALLBACK_SECRET
  DATABASE_URL
  REDIS_URL
  TBANK_TERMINAL_KEY
  TBANK_PASSWORD
  COMET_API_KEY
  KIE_API_KEY
)
missing_runtime_env=()
for name in "${required_runtime_env[@]}"; do
  if [[ -z "${!name:-}" ]]; then
    missing_runtime_env+=("${name}")
  fi
done
if (( ${#missing_runtime_env[@]} > 0 )); then
  printf 'Missing required staging runtime variables: %s\n' "${missing_runtime_env[*]}" >&2
  exit 1
fi

db_url=$(python3 - <<'PY'
import os
url = os.environ["DATABASE_URL"]
for prefix in (
    "postgresql+asyncpg://",
    "postgresql+psycopg://",
    "postgresql+psycopg2://",
):
    if url.startswith(prefix):
        url = "postgresql://" + url[len(prefix):]
        break
print(url)
PY
)

echo "Creating PostgreSQL backup"
pg_dump --format=custom --no-owner --no-privileges \
  --file="${db_backup}" "${db_url}"
test -s "${db_backup}"
pg_restore --list "${db_backup}" > "${backup_dir}/postgres.restore-list.txt"
test -s "${backup_dir}/postgres.restore-list.txt"
sha256sum "${db_backup}" > "${db_backup}.sha256"

echo "Creating application backup"
tar \
  --exclude='./backups' \
  --exclude='./.release' \
  --exclude='./.git' \
  --exclude='./.venv' \
  -czf "${code_backup}" -C "${app_dir}" .
test -s "${code_backup}"
sha256sum "${code_backup}" > "${code_backup}.sha256"

rm -rf "${candidate}"
mkdir -p "${candidate}"
tar -xzf "${archive}" -C "${candidate}"
python3 -m compileall -q "${candidate}/app" "${candidate}/scripts"
chmod 700 "${candidate}/ops/verify_postgres_restore.sh"
"${candidate}/ops/verify_postgres_restore.sh" \
  "${DATABASE_URL}" "${db_url}" "${db_backup}" "${candidate}"

mutation_started=1
rsync --archive --delete \
  --exclude='.env' \
  --exclude='.git/' \
  --exclude='.venv/' \
  --exclude='backups/' \
  --exclude='.release/' \
  "${candidate}/" "${app_dir}/"

cd "${app_dir}"
python3 -m compileall -q app scripts
python3 -m scripts.init_db
python3 scripts/admin_smoke.py
python3 scripts/regression_500.py
python3 scripts/staging_issue3_db_smoke.py
restart_service

health_url=${STUPIDBOT_LOCAL_HEALTH_URL:-http://127.0.0.1:8092/health}
for attempt in $(seq 1 20); do
  if response=$(curl --fail --silent --show-error --max-time 5 "${health_url}") \
    && [[ "${response}" == *'"status":"ok"'* || "${response}" == *'"status": "ok"'* ]]; then
    rollout_succeeded=1
    break
  fi
  sleep 2
done

if (( rollout_succeeded == 0 )); then
  echo "Health check failed: ${health_url}" >&2
  exit 1
fi

python3 scripts/staging_issue3_public_smoke.py
run_root systemctl status "${service_name}" --no-pager --lines=20
journalctl -u "${service_name}" --since "5 minutes ago" --no-pager --lines=100 \
  | sed -E 's/(token|password|secret|api[_-]?key)=([^[:space:]]+)/\1=[REDACTED]/Ig' || true

echo "Backup directory: ${backup_dir}"
echo "Database restore verification: passed"
echo "Transactional financial smoke: passed"
echo "Public Mini App smoke: passed"
echo "Deployed SHA: ${release_sha}"
echo "Automated staging rollout passed"
trap - EXIT
