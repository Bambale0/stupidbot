from __future__ import annotations

import os
from pathlib import Path

from watchfiles import run_process


def _on_reload(changes) -> None:
    changed = ", ".join(path for _, path in sorted(changes))
    print(f"Restarting stupidbot because files changed: {changed}", flush=True)


def main() -> None:
    host = os.getenv("HOST", "0.0.0.0")
    port = os.getenv("PORT", "8000")
    log_level = os.getenv("LOG_LEVEL", "info").lower()
    command = f"uvicorn app.main:app --host {host} --port {port} --log-level {log_level}"
    paths = [str(path) for path in map(Path, ["app", "scripts", "pyproject.toml", ".env"]) if path.exists()]
    run_process(
        *paths,
        target=command,
        target_type="command",
        callback=_on_reload,
    )


if __name__ == "__main__":
    main()
