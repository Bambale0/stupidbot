from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def main() -> None:
    workflow = _read(".github/workflows/release-certification.yml")
    script = _read("ops/certify_release.sh")
    staging_workflow = _read(".github/workflows/staging-rollout.yml")
    staging_script = _read("ops/staging_rollout.sh")
    runbook = _read("docs/release-runbook.md")

    for contract in (
        "name: Release certification",
        "branches: [main, master]",
        "actions: read",
        "issues: write",
        "pull-requests: write",
        "ref: ${{ env.CANDIDATE_SHA }}",
        "ops/certify_release.sh",
        "release-certificate-${{ env.CANDIDATE_SHA }}",
        "retention-days: 90",
    ):
        assert contract in workflow, contract

    for workflow_name in ("CI", "Release contracts", "Financial integrity"):
        assert f'"{workflow_name}"' in script
    for contract in (
        "^[0-9a-f]{40}$",
        "RELEASE_CERTIFICATION_TIMEOUT_SECONDS",
        "deadline=$((SECONDS + timeout_seconds))",
        "while (( SECONDS < deadline )); do",
        "Waiting for exact-SHA workflow evidence",
        "Waiting for exact-SHA staging evidence",
        "find_staging_url()",
        "## Staging rollout: passed",
        "GitHub job status: `success`",
        "Backup/migration/restart/health gate: `passed`",
        "Full readiness/Mini App/package API gate: `passed`",
        'status: "certified"',
        "release-certification:${sha}",
        "candidate_sha",
        "staging_rollout",
    ):
        assert contract in script, contract

    assert script.count("while (( SECONDS < deadline )); do") >= 2
    assert "staging_url=$(find_staging_url)" in script
    assert "Timed out waiting for exact-SHA successful staging evidence" in script

    assert "staging-evidence.json" in staging_workflow
    assert "Full readiness/Mini App/package API gate" in staging_workflow
    assert "https://stupid.chillcreative.ru/ready" in staging_workflow
    assert "https://stupid.chillcreative.ru/miniapp/" in staging_workflow
    assert "https://stupid.chillcreative.ru/api/tma/app/packages" in staging_workflow

    assert "local_readiness_passed=0" in staging_script
    assert "local_readiness_passed=1" in staging_script
    assert "STUPIDBOT_LOCAL_READINESS_URL" in staging_script
    assert 'required = {"database", "redis", "telegram", "tracker"}' in staging_script
    assert "python3 scripts/regression_operations_readiness.py" in staging_script

    for contract in (
        "full 40-character candidate SHA",
        "Release certification",
        "release-evidence.json",
        "main...dev",
        "zero commits in both directions",
    ):
        assert contract in runbook, contract

    forbidden = ("TELEGRAM_BOT_TOKEN=", "TBANK_PASSWORD=", "COMET_API_KEY=", "KIE_API_KEY=")
    for value in forbidden:
        assert value not in script
        assert value not in workflow
        assert value not in runbook

    print("Exact-SHA release certification regression passed")


if __name__ == "__main__":
    main()
