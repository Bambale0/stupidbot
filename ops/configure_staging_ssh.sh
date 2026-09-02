#!/usr/bin/env bash
set -Eeuo pipefail

host=${STAGING_SSH_HOST:?STAGING_SSH_HOST is required}
port=${STAGING_SSH_PORT:-22}
configured_user=${STAGING_SSH_USER:?STAGING_SSH_USER is required}
private_key=${STAGING_SSH_PRIVATE_KEY:?STAGING_SSH_PRIVATE_KEY is required}
expected_fingerprint=${STAGING_SSH_HOST_FINGERPRINT:-SHA256:g2yC4ErjAUcRGnaOYLj/ZgFkJzkn/8w5UvJE/tk9Chg}
repair_public_key_file=${STAGING_SSH_REPAIR_PUBLIC_KEY_FILE:-staging-deployment-key.pub}

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

public_key=$(ssh-keygen -y -f ~/.ssh/id_ed25519)
if [[ -z "${public_key}" ]]; then
  echo "Unable to derive staging deployment public key" >&2
  exit 78
fi
printf '%s stupidbot-github-actions\n' "${public_key}" > "${repair_public_key_file}"
chmod 600 "${repair_public_key_file}"

identity_fingerprint=$(
  ssh-keygen -lf "${repair_public_key_file}" -E sha256 \
    | awk 'NR == 1 { print $2 }'
)
if [[ -z "${identity_fingerprint}" ]]; then
  echo "Unable to fingerprint staging deployment public key" >&2
  exit 78
fi

ssh_options=(
  -i ~/.ssh/id_ed25519
  -o BatchMode=yes
  -o IdentitiesOnly=yes
  -o PreferredAuthentications=publickey
  -o PasswordAuthentication=no
  -o ConnectTimeout=8
  -p "${port}"
)

candidates=("${configured_user}" root ubuntu debian deploy)
declare -A seen=()
selected_user=""
selected_sudo="false"
for candidate in "${candidates[@]}"; do
  [[ -n "${candidate}" ]] || continue
  [[ -z "${seen[${candidate}]:-}" ]] || continue
  seen["${candidate}"]=1

  if ! ssh "${ssh_options[@]}" "${candidate}@${host}" true >/dev/null 2>&1; then
    continue
  fi

  if ssh "${ssh_options[@]}" "${candidate}@${host}" 'test "$(id -u)" -eq 0' >/dev/null 2>&1; then
    selected_user=${candidate}
    selected_sudo="false"
    break
  fi
  if ssh "${ssh_options[@]}" "${candidate}@${host}" 'sudo -n true' >/dev/null 2>&1; then
    selected_user=${candidate}
    selected_sudo="true"
    break
  fi
done

if [[ -z "${selected_user}" ]]; then
  echo "Pinned staging host is reachable, but no approved staging account accepted the deployment key" >&2
  echo "Deployment public-key fingerprint: ${identity_fingerprint}" >&2
  echo "Safe public key for authorized_keys recovery:" >&2
  cat "${repair_public_key_file}" >&2
  echo "Install that single public-key line into the intended staging account's ~/.ssh/authorized_keys, then rerun staging." >&2
  exit 78
fi

if [[ -n "${GITHUB_ENV:-}" ]]; then
  echo "STAGING_SSH_USER=${selected_user}" >> "${GITHUB_ENV}"
  echo "STAGING_SSH_USE_SUDO=${selected_sudo}" >> "${GITHUB_ENV}"
fi

echo "Staging SSH deployment identity verified"
