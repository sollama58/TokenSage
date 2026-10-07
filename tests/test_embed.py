"""The optional embedding classifier (ENABLE_EMBED), with a stubbed encoder: no model file,
no onnxruntime, no network."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest

from tokensage.config import Settings
from tokensage.engine import embed
from tokensage.engine.pipeline import EngineInput, run_basic, run_full
from tokensage.taxonomy import category_labels

# concept -> words the stub treats as that concept; "zorblax" / "quuxle" are invented words the
# rule layers cannot know, so only the embedding stage can place them
CONCEPTS = {
    "dog": ("dog", "puppy", "doggo", "shiba", "zorblax"),
    "cat": ("cat", "kitten", "kitty"),
    "ai": ("intelligence", "agent", "chatbot", "model", "quuxle"),
    "food": ("food", "snack", "drink", "meal"),
}


class StubEncoder:
    """Bag of concepts: each text becomes counts over CONCEPTS plus a small 'noise' dim, so
    related texts point the same way and unrelated ones are near-orthogonal."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def encode(self, texts: list[str]) -> np.ndarray:
        self.calls.append(list(texts))
        out = np.zeros((len(texts), len(CONCEPTS) + 1), dtype=np.float32)
        for i, t in enumerate(texts):
            low = t.lower()
            for j, words in enumerate(CONCEPTS.values()):
                out[i, j] = sum(low.count(w) for w in words)
            out[i, -1] = 0.3  # every text shares a little of this, like real embeddings do
        return out


def _ev(stub: StubEncoder, text: str, where: str = "name"):  # type: ignore[no-untyped-def]
    return embed.evidence(stub, [(where, text)])  # type: ignore[list-item]


def test_yaml_labels_are_taxonomy_labels_and_weights_capped() -> None:
    cfg = embed.load_config()
    assert set(cfg.prompts) <= category_labels()
    assert cfg.max_weight <= 0.4
    assert 0 < cfg.threshold < 1


def test_nearest_label_above_threshold_is_an_embedding_guess() -> None:
    stub = StubEncoder()
    evs = _ev(stub, "a zorblax")
    assert len(evs) == 1
    ev = evs[0]
    assert ev.kind == "embedding" and ev.label == "animal/dog"
    assert ev.detail.startswith("embedding guess")
    assert 0 < ev.weight <= 0.4
    assert ev.referent is None  # categories only, never the referent


def test_no_guess_below_threshold_or_for_tiny_text() -> None:
    stub = StubEncoder()
    assert _ev(stub, "plorp wibble") == []  # matches no concept: only the shared noise dim
    assert _ev(stub, "ab") == []


def test_ambiguous_texts_need_a_margin() -> None:
    stub = StubEncoder()
    # equally dog and cat: neither label wins by min_margin
    assert _ev(stub, "dog cat") == []


def test_weight_rises_with_similarity_but_never_past_cap() -> None:
    c = embed.classifier_for(StubEncoder())
    assert c is not None
    cfg = c.cfg
    assert c.weight(cfg.threshold) == pytest.approx(cfg.weight_floor)
    assert c.weight(0.99) == pytest.approx(cfg.max_weight)
    assert c.weight(cfg.threshold) < c.weight(cfg.full_weight_at - 0.01) <= 0.4


def test_prompt_embeddings_computed_once_per_encoder() -> None:
    stub = StubEncoder()
    _ev(stub, "a zorblax")
    _ev(stub, "a kitten")
    prompt_batches = [c for c in stub.calls if len(c) > 1]
    assert len(prompt_batches) == 1


def test_engine_uses_guesses_only_for_unresolved_tokens() -> None:
    stub = StubEncoder()
    out = run_basic(EngineInput("m", "Zorblax", "ZBX", None, None, None, encoder=stub))
    cats = dict(out.agg.categories)
    assert "animal/dog" in cats and cats["animal/dog"] <= 0.45
    assert any(e.kind == "embedding" for e in out.evidence)
    assert out.agg.referent is None or out.agg.referent.source != "embed:minilm"

    # the lexicon already knows "dog": no embedding rows, no extra encoder calls for texts
    stub2 = StubEncoder()
    out2 = run_basic(EngineInput("m", "Random Dog", "RDOG", None, None, None, encoder=stub2))
    assert not any(e.kind == "embedding" for e in out2.evidence)
    assert stub2.calls == []


def test_engine_embeds_description_and_tweet(monkeypatch: pytest.MonkeyPatch) -> None:
    stub = StubEncoder()
    out = run_basic(
        EngineInput("m", "Plorp", "PLORP", "the first quuxle in town", None, None, encoder=stub)
    )
    rows = [e for e in out.evidence if e.kind == "embedding"]
    assert [(e.where, e.label) for e in rows] == [("description", "ai_agent")]

    from tokensage.engine import xsignals

    real_assess = xsignals.assess

    def assess(*a, **kw):  # type: ignore[no-untyped-def]
        xa = real_assess(*a, **kw)
        xa.text = "look at this zorblax"
        return xa

    monkeypatch.setattr(xsignals, "assess", assess)
    out = run_full(EngineInput("m", "Plorp", "PLORP", None, None, None, x_kind="search"))
    assert not any(e.kind == "embedding" for e in out.evidence)  # no encoder: no guesses
    out = run_full(
        EngineInput("m", "Plorp", "PLORP", None, None, None, x_kind="search", encoder=stub)
    )
    rows = [e for e in out.evidence if e.kind == "embedding"]
    assert ("x", "animal/dog") in [(e.where, e.label) for e in rows]


def test_engine_without_encoder_is_unchanged() -> None:
    a = run_basic(EngineInput("m", "Zorblax", "ZBX", None, None, None))
    assert not any(e.kind == "embedding" for e in a.evidence)
    assert "animal/dog" not in dict(a.agg.categories)


def test_encoder_that_fails_costs_only_its_rows() -> None:
    class Broken:
        def encode(self, texts: list[str]) -> np.ndarray:
            raise RuntimeError("boom")

    out = run_basic(EngineInput("m", "Zorblax", "ZBX", None, None, None, encoder=Broken()))
    assert not any(e.kind == "embedding" for e in out.evidence)


def test_default_encoder_off_by_default_and_tolerates_a_missing_model(tmp_path: Path) -> None:
    assert Settings().enable_embed is False
    assert embed.default_encoder(Settings()) is None
    s = Settings(enable_embed=True, embed_model_path=str(tmp_path / "nope"))
    assert embed.default_encoder(s) is None
    assert "no ONNX model" in (embed.load_error() or "")
    # and the engine still runs with nothing loaded
    out = run_basic(EngineInput("m", "Zorblax", "ZBX", None, None, None, encoder=None))
    assert out.summary


def test_onnxruntime_not_imported_with_the_flag_off() -> None:
    code = (
        "import sys\n"
        "from tokensage.config import Settings\n"
        "from tokensage.engine import embed\n"
        "from tokensage.engine.pipeline import EngineInput, run_basic\n"
        "import tokensage.analyzer\n"
        "assert embed.default_encoder(Settings()) is None\n"
        "run_basic(EngineInput('m', 'Zorblax', 'ZBX', None, None, None))\n"
        "print('onnxruntime' in sys.modules)\n"
    )
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "False"


def test_resolve_paths(tmp_path: Path) -> None:
    (tmp_path / "onnx").mkdir()
    (tmp_path / "onnx" / "model.onnx").write_bytes(b"")
    (tmp_path / "vocab.txt").write_text("[PAD]\n")
    model, vocab = embed.resolve_paths(str(tmp_path))
    assert model == tmp_path / "onnx" / "model.onnx"
    assert vocab == tmp_path / "vocab.txt"  # Hugging Face layout: vocab one level up
    model, vocab = embed.resolve_paths(str(tmp_path / "onnx" / "model.onnx"))
    assert vocab == tmp_path / "vocab.txt"


def test_wordpiece_tokenizer() -> None:
    vocab = ["[PAD]", "[UNK]", "[CLS]", "[SEP]", "dog", "##wif", "##hat", "the", "!", "cafe", "猫"]
    tok = embed.WordPiece({w: i for i, w in enumerate(vocab)})
    v = tok.vocab
    assert tok.encode("Dogwifhat!") == [v["[CLS]"], v["dog"], v["##wif"], v["##hat"], v["!"], 3]
    assert tok.encode("Café") == [2, v["cafe"], 3]  # lowercased, accent stripped
    assert tok.encode("猫 zzz") == [2, v["猫"], v["[UNK]"], 3]
    assert len(tok.encode("the " * 500)) == embed.MAX_TOKENS


def test_onnx_encoder_mean_pools_over_the_mask() -> None:
    class FakeIO:
        def __init__(self, name: str) -> None:
            self.name = name

    class FakeSession:
        def __init__(self) -> None:
            self.feed: dict = {}

        def run(self, names, feed):  # type: ignore[no-untyped-def]
            self.feed = feed
            ids = feed["input_ids"]
            hidden = np.repeat(ids[:, :, None].astype(np.float32), 2, axis=2)
            return [hidden]

    enc = object.__new__(embed.OnnxEncoder)
    enc.session = FakeSession()  # type: ignore[assignment]
    enc.inputs = {"input_ids", "attention_mask", "token_type_ids"}
    enc.outputs = ["last_hidden_state"]
    enc.tok = embed.WordPiece({"[UNK]": 1, "[CLS]": 2, "[SEP]": 4, "a": 6})
    out = enc.encode(["a", "a a a"])
    # row 0: ids [2, 6, 4] (+ padding 0s ignored) -> mean 4; row 1: [2, 6, 6, 6, 4] -> 4.8
    assert out.shape == (2, 2)
    assert out[0, 0] == pytest.approx(4.0) and out[1, 0] == pytest.approx(4.8)
    assert set(enc.session.feed) == {"input_ids", "attention_mask", "token_type_ids"}
