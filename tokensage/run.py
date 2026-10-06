"""Single container entrypoint. The service's role comes from TOKENSAGE_ROLE, so the image
needs no per-service start command (and no shell quoting for a platform to misparse).

    TOKENSAGE_ROLE=api          uvicorn on $PORT (default)
    TOKENSAGE_ROLE=worker       analyzer worker loop
    TOKENSAGE_ROLE=knowledge    daily knowledge cron job
    TOKENSAGE_ROLE=maintenance  hourly maintenance cron job
"""

from __future__ import annotations

import os
import runpy
import sys

MODULES = {
    "worker": "tokensage.worker",
    "knowledge": "tokensage.jobs.knowledge",
    "maintenance": "tokensage.jobs.maintenance",
}
ROLES = ("api", *MODULES)


def serve_api() -> None:
    import uvicorn

    uvicorn.run(
        "tokensage.api.app:app",
        host="0.0.0.0",
        port=int(os.environ.get("PORT") or 10000),
        workers=int(os.environ.get("WEB_CONCURRENCY") or 1),
        proxy_headers=True,
    )


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    role = (args[0] if args else os.environ.get("TOKENSAGE_ROLE") or "api").strip().lower()
    if role == "api":
        serve_api()
        return 0
    module = MODULES.get(role)
    if module is None:
        print(
            f"unknown TOKENSAGE_ROLE {role!r}; expected one of {', '.join(ROLES)}", file=sys.stderr
        )
        return 2
    # Same as `python -m <module>`: runs its `if __name__ == "__main__"` block.
    runpy.run_module(module, run_name="__main__", alter_sys=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
