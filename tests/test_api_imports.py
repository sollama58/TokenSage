"""The API web process (512 MB) must not load the analysis engine just to read versions."""

from __future__ import annotations

import subprocess
import sys

from tokensage import versions


def test_api_app_does_not_import_engine() -> None:
    code = (
        "import sys, tokensage.api.app\n"
        "heavy = ['numpy', 'PIL', 'rapidfuzz', 'tokensage.analyzer', 'tokensage.engine.pipeline',"
        " 'tokensage.fulldepth', 'tokensage.worker']\n"
        "print(','.join(m for m in heavy if m in sys.modules))\n"
    )
    out = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=True, timeout=60
    )
    assert out.stdout.strip() == ""


def test_version_constants_match_engine() -> None:
    from tokensage import analyzer, fulldepth
    from tokensage.engine import pipeline
    from tokensage.engine.knowledge import load_knowledge

    assert pipeline.RULES_VERSION == analyzer.RULES_VERSION == versions.RULES_VERSION
    assert analyzer.LEXICON_VERSION == versions.LEXICON_VERSION
    assert load_knowledge().versions["lexicon"] == versions.LEXICON_VERSION
    assert fulldepth.PAID_X_USAGE_KEY == versions.PAID_X_USAGE_KEY
