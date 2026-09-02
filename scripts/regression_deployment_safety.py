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


def check_release_gate() -> None:
    gate = _read(".github/workflows/release-gate.yml")
    assert gate.count("branches: [main, master]") == 2, "release gate must protect pushes and PRs to main/master"
    assert "uses: ./.github/workflows/ci.yml" in gate
    assert "uses: ./.github/workflows/release-contracts.yml" in gate
    assert "uses: ./.github/workflows/financial-integrity.yml" in gate
    assert "name: Release ready" in gate
    assert "if: always()" in gate, "final release gate must fail explicitly when any dependency fails"
    assert '[[ "${CI_RESULT}" == "success" ]]' in gate
    assert '[[ "${CONTRACTS_RESULT}" == "success" ]]' in gate
    assert '[[ "${FINANCIAL_RESULT}" == "success" ]]' in gate

    deploy = _read(".github/workflows/deploy.yml")
    assert "workflows: [Release gate]" in deploy, "production deploy must depend on the complete release gate"
    assert "github.event.workflow_run.event == 'push'" in deploy, "manual gate runs must not deploy implicitly"
    assert "github.event.workflow_run.head_branch == 'main'" in deploy
    assert "github.ref == 'refs/heads/main'" in deploy, "manual production deploy must be limited to main"
    assert "DEPLOY_SHA: ${{ github.event.workflow_run.head_sha || github.sha }}" in deploy
    assert '"${DEPLOY_SHA}"' in deploy
    assert "git fetch --no-tags --depth=1 origin refs/heads/main" in deploy
    assert 'if [[ "${DEPLOY_SHA}" != "${current_main}" ]]; then' in deploy
    assert '"${DEPLOY_APP_DIR}" "${DEPLOY_SHA}"' in deploy


def check_financial_workflow() -> None:
    workflow = _read(".github/workflows/financial-integrity.yml")
    assert "workflow_call:" in workflow, "financial CI must be reusable by the release gate"
    assert "image: postgres:16" in workflow
    assert "image: redis:7-alpine" in workflow, "financial CI must provide Redis"
    assert "REDIS_URL: redis://127.0.0.1:6379/0" in workflow
    assert "python -m scripts.migrate_db" in workflow
    assert "python scripts/runtime_readiness.py" in workflow
    _assert_pipefail_before_tee(workflow, "python scripts/regression_financial.py")
    _assert_pipefail_before_tee(workflow, "python scripts/regression_500_current.py")
    assert "ops/verify_postgres_restore.sh" in workflow

    duplicated_application_checks = (
        "regression_deployment_safety.py",
        "regression_bot_ux.py",
        "reference_regression.py",
        "regression_backend_contracts.py",
        "regression_gallery_compat.py",
        "regression_telegram_feed_links.py",
        "regression_model_provider_contracts.py",
        "regression_model_env_migration.py",
        "admin_smoke.py",
    )
    for check in duplicated_application_checks:
        assert check not in workflow, f"{check} belongs to Release contracts, not Financial integrity"


def check_reusable_workflows() -> None:
    ci = _read(".github/workflows/ci.yml")
    contracts = _read(".github/workflows/release-contracts.yml")
    financial = _read(".github/workflows/financial-integrity.yml")
    finance_regression = _read("scripts/regression_financial.py")
    assert "workflow_call:" in ci
    assert "workflow_call:" in contracts
    assert "permissions:\n  contents: read" in ci
    assert "permissions:\n  contents: read" in contracts
    assert "permissions:\n  contents: read" in financial

    contract_checks = (
        "regression_deployment_safety.py",
        "regression_bot_ux.py",
        "reference_regression.py",
        "regression_backend_contracts.py",
        "regression_gallery_compat.py",
        "regression_telegram_feed_links.py",
        "regression_model_provider_contracts.py",
        "regression_model_env_migration.py",
        "admin_smoke.py",
    )
    all_layers = ci + contracts + financial
    for check in contract_checks:
        assert check in contracts, f"{check} must run in Release contracts"
        assert all_layers.count(check) == 1, f"{check} must run exactly once in workflow layers"

    assert contracts.count("import scripts.regression_500_current as regression") == 1, "SQLite policy regression must run once"
    assert financial.count("python scripts/regression_500_current.py") == 1, "PostgreSQL policy regression must run once"

    for duplicated in (
        "regression_model_env_migration",
        "regression_model_provider_contracts",
        "regression_telegram_feed_links",
    ):
        assert duplicated not in finance_regression, f"{duplicated} must not be repeated inside financial regression"


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


def check_http_readiness_contract() -> None:
    bot_source = _read("app/bot.py")
    readiness_source = _read("app/readiness.py")
    public_smoke = _read("scripts/staging_issue3_public_smoke.py")
    assert "install_http_readiness_route()" in bot_source
    assert '"/ready"' in readiness_source
    assert "request.app.state.engine" in readiness_source
    assert "request.app.state.redis" in readiness_source
    assert "request.app.state.tracker" in readiness_source
    assert 'client.get("/ready")' in public_smoke
    assert '"tracker": "ok"' in public_smoke


def check_default_ci() -> None:
    script = _read("scripts/ci.sh")
    workflow = _read(".github/workflows/ci.yml")
    assert "set -euo pipefail" in script
    assert "python3 -m compileall -q app scripts" in script
    assert "python3 -m pip check" in script
    assert "ruff check --select E9,F63,F7,F82 app scripts" in script
    assert "TELEGRAM_BOT_TOKEN" not in workflow, "base CI must not carry fake application credentials"
    assert "DATABASE_URL" not in workflow, "base CI must stay independent of database backends"


if __name__ == "__main__":
    check_release_gate()
    check_financial_workflow()
    check_reusable_workflows()
    check_rollout()
    check_http_readiness_contract()
    check_default_ci()
    from scripts.regression_http_readiness import amain as readiness_regression

    asyncio.run(readiness_regression())
    print("Deployment safety regression passed")
