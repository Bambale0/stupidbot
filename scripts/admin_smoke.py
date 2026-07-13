from __future__ import annotations

import app.bot  # noqa: F401,E402

from scripts.admin_smoke_current import amain, main  # noqa: E402

__all__ = ["amain", "main"]


if __name__ == "__main__":
    main()
