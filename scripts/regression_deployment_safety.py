from __future__ import annotations

import asyncio
from pathlib import Path

if __package__ in {None, ""}:
    from _bootstrap import add_project_root_to_path

    add_project_root_to_path()

ROOT = Path(__file__).resolve().parents[1]


def _read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def _assert_pipefail_before_tee(workflow: str, command: str) -> None:
    command_index = workflow.index(command)
    block_start = workflow.rfind("- name:", 0, command_index)
    block = workflow[block_start:command_index]
    assert "set -Eeuo pipefail" in block, f"{command} must run with pipefail"


def check_financial_workflow() -> None:
    workflow = _read(".github/workflows/financial-integrity.yml")
    trigger = "branches: [dev, main, master]"
    assert workflow.count(trigger) == 2, "financial CI must protect push and PRs for dev/main/master"
    assert "image: redis:7-alpine" in workflow, "financial CI must provide Redis"
    assert "REDIS_URL: redis://127.0.0.1:6379/0" in workflow
    _assert_pipefail_before_tee(workflow, "python scripts/regression_financial.py")
    _assert_pipefail_before_tee(workflow, "python scripts/regression_500_current.py")
    assert "python scripts/regression_deployment_safety.py" in workflow
    assert "python scripts/regression_operations_readiness.py" in workflow
    assert "python scripts/runtime_readiness.py" in workflow


def check_rollout() -> None:
    rollout = _read("ops/staging_rollout.sh")
    assert "local_health_passed=0" in rollout
    assert "local_health_passed=1" in rollout
    assert "if (( local_health_passed == 0 )); then" in rollout
    assert "local_readiness_passed=0" in rollout
    assert "local_readiness_passed=1" in rollout
    assert "if (( local_readiness_passed == 0 )); then" in rollout
    assert "STUPIDBOT_LOCAL_READINESS_URL" in rollout

    mutation_index = rollout.index("mutation_started=1")
    candidate_safety_index = rollout.index("python3 scripts/regression_deployment_safety.py")
    candidate_runtime_index = rollout.index("python3 scripts/runtime_readiness.py")
    candidate_operations_index = rollout.index("python3 scripts/regression_operations_readiness.py")
    assert candidate_safety_index < mutation_index, "candidate safety gate must run before rsync mutation"
    assert candidate_runtime_index < mutation_index, "candidate runtime check must run before rsync mutation"
    assert candidate_operations_index < mutation_index, "operations contract must run before rsync mutation"

    assert rollout.count("python3 scripts/runtime_readiness.py") >= 3
    assert "restart_service\npython3 scripts/runtime_readiness.py" in rollout

    public_smoke_index = rollout.index("python3 scripts/staging_issue3_public_smoke.py")
    success_index = rollout.index("rollout_succeeded=1")
    assert public_smoke_index < success_index, "rollback must stay armed through public smoke"

    readiness_index = rollout.index("readiness_url=${STUPIDBOT_LOCAL_READINESS_URL")
    assert readiness_index < public_smoke_index, "local full readiness must pass before public smoke"

    status_index = rollout.index('run_root systemctl status "${service_name}"')
    assert status_index < success_index, "rollback must stay armed through service status verification"


def check_staging_evidence() -> None:
    workflow = _read(".github/workflows/staging-rollout.yml")
    assert "Probe public liveness and readiness from GitHub runner" in workflow
    assert "Probe public Mini App and package API from GitHub runner" in workflow
    assert workflow.count("continue-on-error: true") >= 2
    assert "Create exact-SHA staging evidence" in workflow
    assert "staging-evidence.json" in workflow
    assert "retention-days: 90" in workflow
    assert "Full readiness/Mini App/package API gate" in workflow
    assert ".checks.telegram == \"ok\"" in workflow
    assert "all(.items[]; ((.price_rub | tonumber) > 0))" in workflow
    for marker in (
        "Database restore verification: passed",
        "Runtime dependency readiness: passed",
        "HTTP readiness endpoint: passed",
        "Public Mini App smoke: passed",
        "Automated staging rollout passed",
    ):
        assert f"grep -q '{marker}' staging-rollout.log" in workflow
    assert "authoritative gate runs from the deployed staging host" in workflow
    assert "github_runner_runtime_probe" in workflow
    assert "github_runner_product_probe" in workflow


def check_staging_ssh_pin() -> None:
    workflow = _read(".github/workflows/staging-rollout.yml")
    helper = _read("ops/configure_staging_ssh.sh")
    fingerprint = "SHA256:g2yC4ErjAUcRGnaOYLj/ZgFkJzkn/8w5UvJE/tk9Chg"

    assert f"STAGING_SSH_HOST_FINGERPRINT: {fingerprint}" in workflow
    assert "ops/configure_staging_ssh.sh" in workflow
    assert "STAGING_SSH_KNOWN_HOSTS" not in workflow
    assert "ssh-keyscan" in helper
    assert "ssh-keygen -lf" in helper
    assert "actual_fingerprint" in helper
    assert "expected_fingerprint" in helper
    assert "Staging host key fingerprint mismatch" in helper
    assert "StrictHostKeyChecking=no" not in workflow
    assert "StrictHostKeyChecking=no" not in helper


def check_staging_ssh_identity_recovery() -> None:
    workflow = _read(".github/workflows/staging-rollout.yml")
    helper = _read("ops/configure_staging_ssh.sh")

    for contract in (
        "BatchMode=yes",
        "IdentitiesOnly=yes",
        "PreferredAuthentications=publickey",
        "PasswordAuthentication=no",
        'candidates=("${configured_user}" root ubuntu debian deploy)',
        "sudo -n true",
        "STAGING_SSH_USE_SUDO",
        "Deployment public-key fingerprint",
        "ssh-keygen -y -f ~/.ssh/id_ed25519",
        "Safe public key for authorized_keys recovery",
        "stupidbot-github-actions",
    ):
        assert contract in helper, contract

    assert "STAGING_SSH_USE_SUDO" in workflow
    assert "sudo -n mkdir -p" in workflow
    assert "sudo -n bash -s --" in workflow
    assert "sshpass" not in helper
    assert "sshpass" not in workflow
    assert "PasswordAuthentication=yes" not in helper
    assert "PasswordAuthentication=yes" not in workflow
    assert "StrictHostKeyChecking=no" not in helper
    assert "StrictHostKeyChecking=no" not in workflow
    assert "BEGIN OPENSSH PRIVATE KEY" not in helper
    assert "cat ~/.ssh/id_ed25519" not in helper


def check_http_readiness_contract() -> None:
    bot_source = _read("app/bot.py")
    readiness_source = _read("app/readiness.py")
    operations_source = _read("app/operations.py")
    public_smoke = _read("scripts/staging_issue3_public_smoke.py")
    assert "install_http_readiness_route()" in bot_source
    assert "install_http_operations_routes()" in bot_source
    assert '"/ready"' in readiness_source
    assert "request.app.state.engine" in readiness_source
    assert "request.app.state.redis" in readiness_source
    assert "request.app.state.bot" in readiness_source or 'getattr(request.app.state, "bot"' in readiness_source
    assert "request.app.state.tracker" in readiness_source
    assert "_check_telegram" in readiness_source
    assert 'OPERATIONS_METRICS_PATH = "/ops/metrics"' in operations_source
    assert "hmac.compare_digest(supplied, expected)" in operations_source
    assert 'client.get("/ready")' in public_smoke
    assert '"telegram": "ok"' in public_smoke
    assert '"tracker": "ok"' in public_smoke


def check_default_ci() -> None:
    script = _read("scripts/ci.sh")
    assert "set -euo pipefail" in script
    assert "python3 scripts/regression_deployment_safety.py" in script
    assert "python3 scripts/regression_release_certification.py" in script


if __name__ == "__main__":
    check_financial_workflow()
    check_rollout()
    check_staging_evidence()
    check_staging_ssh_pin()
    check_staging_ssh_identity_recovery()
    check_http_readiness_contract()
    check_default_ci()
    from scripts.regression_http_readiness import amain as readiness_regression
    from scripts.regression_release_certification import main as release_certification_regression

    asyncio.run(readiness_regression())
    release_certification_regression()
    print("Deployment safety regression passed")
