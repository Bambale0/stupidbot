from __future__ import annotations

import asyncio

if __package__ in {None, ""}:
    from _bootstrap import add_project_root_to_path

    add_project_root_to_path()

from app.config import get_settings
from app.db import build_engine, build_session_factory, session_scope
from app.repositories import ensure_defaults
from scripts.migrate_db import run_migrations


async def seed_defaults() -> None:
    settings = get_settings()
    engine = build_engine(settings)
    session_factory = build_session_factory(engine)
    async with session_scope(session_factory) as session:
        await ensure_defaults(session, settings.admin_ids)
    await engine.dispose()


def main() -> None:
    run_migrations()
    asyncio.run(seed_defaults())
    print("Database initialized.")


if __name__ == "__main__":
    main()
