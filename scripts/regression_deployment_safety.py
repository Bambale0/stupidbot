from __future__ import annotations

from pathlib import Path

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
    assert "python scripts/runtime_readiness.py" in workflow


def check_rollout() -> None:
    rollout = _read("ops/staging_rollout.sh")
    assert "local_health_passed=0" in rollout
    assert "local_health_passed=1" in rollout
    assert "if (( local_health_passed == 0 )); then" in rollout

    mutation_index = rollout.index("mutation_started=1")
    candidate_safety_index = rollout.index("python3 scripts/regression_deployment_safety.py")
    candidate_runtime_index = rollout.index("python3 scripts/runtime_readiness.py")
    assert candidate_safety_index < mutation_index, "candidate safety gate must run before rsync mutation"
    assert candidate_runtime_index < mutation_index, "candidate runtime check must run before rsync mutation"

    assert rollout.count("python3 scripts/runtime_readiness.py") >= 3
    assert "restart_service\npython3 scripts/runtime_readiness.py" in rollout

    public_smoke_index = rollout.index("python3 scripts/staging_issue3_public_smoke.py")
    success_index = rollout.index("rollout_succeeded=1")
    assert public_smoke_index < success_index, "rollback must stay armed through public smoke"

    status_index = rollout.index('run_root systemctl status "${service_name}"')
    assert status_index < success_index, "rollback must stay armed through service status verification"


def check_default_ci() -> None:
    script = _read("scripts/ci.sh")
    assert "set -euo pipefail" in script
    assert "python3 scripts/regression_deployment_safety.py" in script


if __name__ == "__main__":
    check_financial_workflow()
    check_rollout()
    check_default_ci()
    print("Deployment safety regression passed")
