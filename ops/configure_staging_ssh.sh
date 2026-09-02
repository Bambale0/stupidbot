#!/usr/bin/env bash
set -Eeuo pipefail

host=${STAGING_SSH_HOST:?STAGING_SSH_HOST is required}
port=${STAGING_SSH_PORT:-22}
private_key=${STAGING_SSH_PRIVATE_KEY:?STAGING_SSH_PRIVATE_KEY is required}
expected_fingerprint=${STAGING_SSH_HOST_FINGERPRINT:-SHA256:g2yC4ErjAUcRGnaOYLj/ZgFkJzkn/8w5UvJE/tk9Chg}

install -d -m 700 ~/.ssh
printf '%s\n' "${private_key}" > ~/.ssh/id_ed25519
chmod 600 ~/.ssh/id_ed25519

scan_file=$(mktemp)
trap 'rm -f "${scan_file}"' EXIT

if ! ssh-keyscan -T 10 -p "${port}" -t ed25519 "${host}" 2>/dev/null > "${scan_file}"; then
  echo "Unable to read staging ED25519 host key" >&2
  exit 1
fi
if [[ ! -s "${scan_file}" ]]; then
  echo "Staging ED25519 host key scan returned no keys" >&2
  exit 1
fi

actual_fingerprint=$(
  ssh-keygen -lf "${scan_file}" -E sha256 \
    | awk 'NR == 1 { print $2 }'
)
if [[ -z "${actual_fingerprint}" ]]; then
  echo "Unable to calculate staging host key fingerprint" >&2
  exit 1
fi
if [[ "${actual_fingerprint}" != "${expected_fingerprint}" ]]; then
  echo "Staging host key fingerprint mismatch" >&2
  echo "Expected: ${expected_fingerprint}" >&2
  echo "Observed: ${actual_fingerprint}" >&2
  exit 78
fi

cp "${scan_file}" ~/.ssh/known_hosts
chmod 600 ~/.ssh/known_hosts

ssh-keygen -F "${host}" -f ~/.ssh/known_hosts >/dev/null 2>&1 \
  || ssh-keygen -F "[${host}]:${port}" -f ~/.ssh/known_hosts >/dev/null 2>&1

echo "Staging SSH host key verified against pinned SHA256 fingerprint"
