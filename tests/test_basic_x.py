"""Basic-depth X interpretation (rules 0.36.0): post text in the engine without full depth."""

from __future__ import annotations

from datetime import UTC, datetime

from tokensage.engine.pipeline import EngineInput, run_basic
from tokensage.sources.x import TweetData

WHEN = datetime(2026, 10, 7, 19, 30, tzinfo=UTC)


def test_basic_run_uses_tweet_text_for_post_subject() -> None:
    tweet = TweetData(
        id="18446744073709551616",
        status="ok",
        source="test",
        text='Everyone meet "Luna" the white lioness',
        author_handle="zoo",
        created_at=WHEN,
    )
    out = run_basic(
        EngineInput(
            "M",
            "Luna",
            "LUNA",
            None,
            None,
            WHEN,
            x_kind="tweet",
            x_url_handle="zoo",
            tweet=tweet,
        )
    )
    assert out.x is not None and out.x.status == "ok"
    rr = out.referent_read
    assert rr is not None
    assert rr.source == "x"
    assert "luna" in rr.label.lower()
    assert rr.post_id == tweet.id


def test_basic_without_tweet_has_no_x_assessment() -> None:
    out = run_basic(EngineInput("M", "Luna", "LUNA", None, None, WHEN, x_kind="tweet"))
    assert out.x is None
