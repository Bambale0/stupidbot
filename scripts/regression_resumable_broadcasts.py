from __future__ import annotations

from pathlib import Path

from app.plugins.loader import normalized_plugin_names


def main() -> None:
    names = normalized_plugin_names(["core", "admin", "ux"])
    assert names.index("admin_broadcast") < names.index("admin")
    assert names.index("admin_finance") < names.index("admin")

    source = Path("app/plugins/admin_broadcast/plugin.py").read_text(encoding="utf-8")
    required = (
        "StateFilter(AdminStates.broadcast_text)",
        "broadcast:{broadcast_id}:sent",
        "broadcast:{broadcast_id}:failures",
        "broadcast:{broadcast_id}:lock",
        "nx=True",
        "status=\"interrupted\"",
        "status=\"failed\"",
        "admin:broadcast:resume:",
        "admin:broadcast:diagnostics:",
        "await context.redis.sismember",
        "await context.redis.sadd",
        "except Exception as exc",
    )
    for token in required:
        assert token in source, token

    send_loop = source.split("async def _run_broadcast", 1)[1]
    assert "for user in users:" in send_loop
    assert send_loop.index("try:") < send_loop.index("except Exception as exc")
    assert "logger.warning(" in send_loop
    assert "raw init" not in source.lower()
    assert "telegram_bot_token" not in source

    print("resumable broadcast regression passed")


if __name__ == "__main__":
    main()
