from __future__ import annotations

from pathlib import Path

if __package__ in {None, ""}:
    from _bootstrap import add_project_root_to_path

    add_project_root_to_path()

from alembic import command
from alembic.config import Config


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def alembic_config() -> Config:
    config = Config(str(PROJECT_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(PROJECT_ROOT / "alembic"))
    return config


def run_migrations() -> None:
    command.upgrade(alembic_config(), "head")


def main() -> None:
    run_migrations()
    print("Database migrated.")


if __name__ == "__main__":
    main()
