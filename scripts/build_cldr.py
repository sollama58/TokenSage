"""Build data/cldr_emoji_en.json: emoji -> keyword list, from Unicode CLDR annotations.

Source: https://raw.githubusercontent.com/unicode-org/cldr-json/main/cldr-json/
        cldr-annotations-full/annotations/en/annotations.json   (Unicode licence)

    python scripts/build_cldr.py [path/to/annotations.json]

Downloads the file when no path is given. We keep only the keywords (CLDR "default") plus
the TTS name, and strip variation selectors from keys so lookups work on bare code points.
"""

from __future__ import annotations

import json
import sys
import urllib.request
from pathlib import Path

URL = (
    "https://raw.githubusercontent.com/unicode-org/cldr-json/main/cldr-json/"
    "cldr-annotations-full/annotations/en/annotations.json"
)
OUT = Path(__file__).resolve().parent.parent / "data" / "cldr_emoji_en.json"
VS = {"️", "︎"}


def main() -> int:
    if len(sys.argv) > 1:
        raw = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    else:
        with urllib.request.urlopen(URL, timeout=30) as r:  # noqa: S310
            raw = json.load(r)
    ann = raw["annotations"]["annotations"]
    out: dict[str, list[str]] = {}
    for emoji, v in ann.items():
        key = "".join(ch for ch in emoji if ch not in VS)
        kws = list(dict.fromkeys([*(v.get("tts") or []), *(v.get("default") or [])]))
        kws = [k.strip().lower() for k in kws if k.strip()]
        if kws:
            out.setdefault(key, kws)
    OUT.write_text(json.dumps(out, ensure_ascii=False, separators=(",", ":")) + "\n", "utf-8")
    print(f"wrote {OUT} ({len(out)} emoji)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
