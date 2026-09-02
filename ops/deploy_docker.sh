#!/usr/bin/env bash
set -Eeuo pipefail

app_dir=${1:-/root/stupidbot}
release_sha=${2:?release SHA is required}
compose_file=${STUPIDBOT_COMPOSE_FILE:-docker-compose.runtime.yml}
release_root="${app_dir}/.release"
archive="${release_root}/stupidbot-source.tar.gz"
checksum="${archive}.sha256"
candidate="${release_root}/candidate-${release_sha}"
timestamp=$(date -u +%Y%m%dT%H%M%SZ)
backup_dir="${app_dir}/backups/${timestamp}"
code_backup="${backup_dir}/app-code.tar.gz"
restore_root="${release_root}/rollback-${release_sha}"
mutation_started=0
rollout_succeeded=0

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
  (
    cd "${app_dir}"
    docker compose --project-directory "${app_dir}" -f "${compose_file}" build app
    docker compose --project-directory "${app_dir}" -f "${compose_file}" up -d app
  ) || true
}

on_exit() {
  status=$?
  if (( status != 0 )) && (( mutation_started == 1 )) && (( rollout_succeeded == 0 )); then
    echo "Rollout failed; restoring previous code. Backup remains at ${backup_dir}" >&2
    rollback_code || true
  fi
  exit "${status}"
}
trap on_exit EXIT

[[ -d "${app_dir}" ]] || { echo "Application directory does not exist: ${app_dir}" >&2; exit 1; }
[[ -f "${app_dir}/.env" ]] || { echo "Missing ${app_dir}/.env" >&2; exit 1; }
[[ -s "${archive}" ]] || { echo "Missing candidate archive" >&2; exit 1; }
[[ -s "${checksum}" ]] || { echo "Missing candidate checksum" >&2; exit 1; }
command -v docker >/dev/null
command -v rsync >/dev/null
command -v curl >/dev/null

umask 077
mkdir -p "${backup_dir}" "${release_root}"

cd "${release_root}"
sha256sum --check "$(basename "${checksum}")"

rm -rf "${candidate}"
mkdir -p "${candidate}"
tar -xzf "${archive}" -C "${candidate}"
python3 -m compileall -q "${candidate}/app" "${candidate}/scripts"

echo "Creating application backup"
tar \
  --exclude='./backups' \
  --exclude='./.release' \
  --exclude='./.git' \
  --exclude='./.venv' \
  -czf "${code_backup}" -C "${app_dir}" .
test -s "${code_backup}"
sha256sum "${code_backup}" > "${code_backup}.sha256"

mutation_started=1
rsync --archive --delete \
  --exclude='.env' \
  --exclude='.git/' \
  --exclude='.venv/' \
  --exclude='backups/' \
  --exclude='.release/' \
  "${candidate}/" "${app_dir}/"

cd "${app_dir}"
echo "Building application image"
docker compose --project-directory "${app_dir}" -f "${compose_file}" build app

echo "Applying database migrations"
docker compose --project-directory "${app_dir}" -f "${compose_file}" run --rm app \
  python -m scripts.migrate_db

echo "Restarting application"
docker compose --project-directory "${app_dir}" -f "${compose_file}" up -d app

health_passed=0
for attempt in $(seq 1 30); do
  health=$(docker inspect --format '{{.State.Health.Status}}' stupidbot-app-1 2>/dev/null || true)
  if [[ "${health}" == "healthy" ]]; then
    health_passed=1
    break
  fi
  sleep 5
done

if (( health_passed == 0 )); then
  echo "Container health check failed" >&2
  docker logs stupidbot-app-1 --tail 100 2>&1 \
    | sed -E 's/(token|password|secret|api[_-]?key)=[^[:space:]]+/\1=[REDACTED]/Ig' >&2 || true
  exit 1
fi

docker logs stupidbot-app-1 --tail 20 2>&1 \
  | sed -E 's/(token|password|secret|api[_-]?key)=[^[:space:]]+/\1=[REDACTED]/Ig' || true

rollout_succeeded=1
echo "Backup directory: ${backup_dir}"
echo "Automated docker rollout passed"
