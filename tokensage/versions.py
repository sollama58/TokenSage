"""Version strings and shared keys, kept free of heavy imports.

The API web process reads these without importing the engine (numpy, PIL, rapidfuzz, ...),
which it never runs unless INLINE_ANALYZER is on.
"""

from __future__ import annotations

RULES_VERSION = "0.37.0-full"
LEXICON_VERSION = "2026-10-09.2"
KNOWN_COINS_VERSION = "seed-2026-10-06"

PAID_X_USAGE_KEY = "_paid_x"  # api_usage row counting paid X calls per UTC day
NOW_KEY = "now:"  # lookup_cache key prefix: a mint's latest decision-time refresh
NOW_USAGE_KEY = "_now:"  # api_usage row per key (+ its name): `now` jobs started per UTC day
# lookup_cache key prefix: the referents a mint's latest full read matched against the trends,
# which its stored document does not keep (only the chosen one); read by the `now` refresh
TREND_INPUTS_KEY = "trendin:"
