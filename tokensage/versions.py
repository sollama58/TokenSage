"""Version strings and shared keys, kept free of heavy imports.

The API web process reads these without importing the engine (numpy, PIL, rapidfuzz, ...),
which it never runs unless INLINE_ANALYZER is on.
"""

from __future__ import annotations

RULES_VERSION = "0.25.0-full"
LEXICON_VERSION = "2026-10-07.3"
KNOWN_COINS_VERSION = "seed-2026-10-06"

PAID_X_USAGE_KEY = "_paid_x"  # api_usage row counting paid X calls per UTC day
