"""tokensage.run dispatches on TOKENSAGE_ROLE (or argv) without any shell."""

from __future__ import annotations

from typing import Any

import pytest

from tokensage import run


@pytest.mark.parametrize(
    ("role", "module"),
    [
        ("worker", "tokensage.worker"),
        ("knowledge", "tokensage.jobs.knowledge"),
        ("maintenance", "tokensage.jobs.maintenance"),
        ("  Worker ", "tokensage.worker"),
    ],
)
def test_roles_run_their_module_as_main(
    monkeypatch: pytest.MonkeyPatch, role: str, module: str
) -> None:
    calls: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(run.runpy, "run_module", lambda m, **kw: calls.append((m, kw)))
    monkeypatch.setenv("TOKENSAGE_ROLE", role)
    assert run.main([]) == 0
    assert calls == [(module, {"run_name": "__main__", "alter_sys": True})]


def test_default_role_is_api_and_reads_port(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}
    import uvicorn

    monkeypatch.setattr(uvicorn, "run", lambda app, **kw: seen.update(app=app, **kw))
    monkeypatch.delenv("TOKENSAGE_ROLE", raising=False)
    monkeypatch.setenv("PORT", "12345")
    monkeypatch.delenv("WEB_CONCURRENCY", raising=False)
    assert run.main([]) == 0
    assert seen == {
        "app": "tokensage.api.app:app",
        "host": "0.0.0.0",
        "port": 12345,
        "workers": 1,
        "proxy_headers": True,
    }


def test_argv_overrides_env_and_unknown_role_fails(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    calls: list[str] = []
    monkeypatch.setattr(run.runpy, "run_module", lambda m, **kw: calls.append(m))
    monkeypatch.setenv("TOKENSAGE_ROLE", "api")
    assert run.main(["maintenance"]) == 0
    assert calls == ["tokensage.jobs.maintenance"]
    assert run.main(["nope"]) == 2
    assert "unknown TOKENSAGE_ROLE" in capsys.readouterr().err
