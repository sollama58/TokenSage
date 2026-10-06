"""Write the OpenAPI schema to openapi.v1.json (committed; CI diffs it for compatibility)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

from tokensage.api.app import create_app
from tokensage.config import Settings

OUT = Path(__file__).resolve().parent.parent / "openapi.v1.json"


def main() -> int:
    app = create_app(Settings(_env_file=None))  # type: ignore[call-arg]
    schema = app.openapi()
    text = json.dumps(schema, indent=2, sort_keys=True) + "\n"
    if "--check" in sys.argv:
        if OUT.read_text() != text:
            print(
                "openapi.v1.json is stale; run: python scripts/export_openapi.py", file=sys.stderr
            )
            return 1
        print("openapi.v1.json is up to date")
        return 0
    OUT.write_text(text)
    print(f"wrote {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
