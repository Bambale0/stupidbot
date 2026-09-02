#!/usr/bin/env bash
set -Eeuo pipefail

repo=${1:?repository owner/name is required}
sha=${2:?candidate SHA is required}
pr_number=${3:?release pull request number is required}
staging_issue=${4:-17}
output=${5:-release-evidence.json}
timeout_seconds=${RELEASE_CERTIFICATION_TIMEOUT_SECONDS:-1200}
sleep_seconds=${RELEASE_CERTIFICATION_SLEEP_SECONDS:-15}
deadline=$((SECONDS + timeout_seconds))

if [[ ! "${sha}" =~ ^[0-9a-f]{40}$ ]]; then
  echo "Candidate SHA must be a full 40-character lowercase commit SHA" >&2
  exit 2
fi
if [[ -z "${GH_TOKEN:-}" ]]; then
  echo "GH_TOKEN is required" >&2
  exit 2
fi
if (( timeout_seconds < 60 )); then
  echo "RELEASE_CERTIFICATION_TIMEOUT_SECONDS must be at least 60" >&2
  exit 2
fi
if (( sleep_seconds < 1 )); then
  echo "RELEASE_CERTIFICATION_SLEEP_SECONDS must be positive" >&2
  exit 2
fi
command -v gh >/dev/null
command -v jq >/dev/null

workflow_names=("CI" "Release contracts" "Financial integrity")
declare -A workflow_urls=()
workflow_evidence_ready=0

while (( SECONDS < deadline )); do
  runs_json=$(gh api --method GET "repos/${repo}/actions/runs" \
    -f head_sha="${sha}" \
    -f per_page=100)
  pending=0

  for workflow_name in "${workflow_names[@]}"; do
    row=$(jq -c --arg name "${workflow_name}" '
      [.workflow_runs[]
        | select(.name == $name)
        | select(.event == "pull_request" or .event == "push" or .event == "workflow_dispatch")]
      | sort_by(.run_number)
      | last // empty
    ' <<<"${runs_json}")

    if [[ -z "${row}" ]]; then
      pending=1
      continue
    fi

    status=$(jq -r '.status // "missing"' <<<"${row}")
    conclusion=$(jq -r '.conclusion // "pending"' <<<"${row}")
    run_url=$(jq -r '.html_url // empty' <<<"${row}")

    if [[ "${status}" != "completed" ]]; then
      pending=1
      continue
    fi
    if [[ "${conclusion}" != "success" ]]; then
      echo "${workflow_name} completed with conclusion ${conclusion} for ${sha}" >&2
      exit 1
    fi
    workflow_urls["${workflow_name}"]="${run_url}"
  done

  if (( pending == 0 )); then
    workflow_evidence_ready=1
    echo "Exact-SHA CI, Release contracts and Financial integrity evidence is ready"
    break
  fi
  echo "Waiting for exact-SHA workflow evidence for ${sha}"
  sleep "${sleep_seconds}"
done

if (( workflow_evidence_ready == 0 )); then
  echo "Timed out waiting for exact-SHA workflow evidence for ${sha}" >&2
  exit 1
fi

find_staging_url() {
  gh api --paginate "repos/${repo}/issues/${staging_issue}/comments?per_page=100" \
    | jq -r --arg sha "${sha}" '
        .[]
        | select((.body // "") | contains($sha))
        | select((.body // "") | contains("## Staging rollout: passed"))
        | select((.body // "") | contains("GitHub job status: `success`"))
        | select((.body // "") | contains("Backup/migration/restart/health gate: `passed`"))
        | select((.body // "") | contains("Full readiness/Mini App/package API gate: `passed`"))
        | .html_url
      ' \
    | tail -1
}

staging_url=""
while (( SECONDS < deadline )); do
  staging_url=$(find_staging_url)
  if [[ -n "${staging_url}" ]]; then
    echo "Exact-SHA staging evidence is ready"
    break
  fi
  echo "Waiting for exact-SHA staging evidence for ${sha}"
  sleep "${sleep_seconds}"
done

if [[ -z "${staging_url}" ]]; then
  echo "Timed out waiting for exact-SHA successful staging evidence for ${sha}" >&2
  exit 1
fi

created_at=$(date -u +%Y-%m-%dT%H:%M:%SZ)
jq -n \
  --arg repository "${repo}" \
  --arg sha "${sha}" \
  --argjson pull_request "${pr_number}" \
  --arg created_at "${created_at}" \
  --arg ci "${workflow_urls[CI]}" \
  --arg release_contracts "${workflow_urls[Release contracts]}" \
  --arg financial_integrity "${workflow_urls[Financial integrity]}" \
  --arg staging "${staging_url}" \
  '{
    schema_version: 1,
    repository: $repository,
    candidate_sha: $sha,
    pull_request: $pull_request,
    created_at: $created_at,
    status: "certified",
    checks: {
      ci: {status: "passed", evidence: $ci},
      release_contracts: {status: "passed", evidence: $release_contracts},
      financial_integrity: {status: "passed", evidence: $financial_integrity},
      staging_rollout: {status: "passed", evidence: $staging}
    }
  }' > "${output}"

marker="<!-- release-certification:${sha} -->"
comment_file=$(mktemp)
comment_json=$(mktemp)
trap 'rm -f "${comment_file}" "${comment_json}"' EXIT
cat > "${comment_file}" <<EOF
${marker}
## Release candidate certified

- Candidate SHA: \`${sha}\`
- CI: passed
- Release contracts: passed
- Financial integrity: passed
- Exact-SHA staging rollout: passed

The machine-readable certificate is attached to the Release certification workflow artifact.
EOF
jq -Rs '{body: .}' < "${comment_file}" > "${comment_json}"

existing_comment_id=$(
  gh api --paginate "repos/${repo}/issues/${pr_number}/comments?per_page=100" \
    | jq -r --arg marker "${marker}" '.[] | select((.body // "") | contains($marker)) | .id' \
    | tail -1
)
if [[ -n "${existing_comment_id}" ]]; then
  gh api --method PATCH "repos/${repo}/issues/comments/${existing_comment_id}" \
    --input "${comment_json}" >/dev/null
else
  gh api --method POST "repos/${repo}/issues/${pr_number}/comments" \
    --input "${comment_json}" >/dev/null
fi

echo "Release candidate ${sha} certified"
