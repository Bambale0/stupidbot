from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MAIN = ROOT / "app" / "main.py"

HEALTH_BLOCK = '''    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}
'''

READY_BLOCK = '''    @app.get("/ready")
    async def ready(request: Request) -> dict[str, Any]:
        from app.readiness import readiness_response

        return await readiness_response(request)
'''


def main() -> None:
    source = MAIN.read_text(encoding="utf-8")
    if '@app.get("/ready")' in source:
        print("HTTP readiness route already installed")
        return
    if source.count(HEALTH_BLOCK) != 1:
        raise RuntimeError("expected one canonical /health block in app/main.py")
    source = source.replace(HEALTH_BLOCK, f"{HEALTH_BLOCK}\n{READY_BLOCK}", 1)
    MAIN.write_text(source, encoding="utf-8")
    print("Installed /ready route in app/main.py")


if __name__ == "__main__":
    main()
