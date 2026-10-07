"""Optional embedding classifier (flag ENABLE_EMBED, guide §5.6a).

A local MiniLM-class sentence encoder in ONNX embeds the token's name, description and linked
tweet; each text is given the taxonomy label whose prompt (data/embed_labels.yaml) is nearest
by cosine, when it clears the threshold. The rows are marked "embedding guess", weigh at most
0.4 and never name a referent. Local inference only (like OCR), so it is within the
no-external-AI rule.

The engine works fully without it: with the flag off nothing here is loaded, and onnxruntime
is imported only when the flag is on and a model is configured. Never raises.
"""

from __future__ import annotations

import threading
import unicodedata
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

import numpy as np
import structlog
import yaml

from tokensage.engine.context import Ev, Where
from tokensage.taxonomy import DATA_DIR, category_labels

if TYPE_CHECKING:
    from tokensage.config import Settings

MAX_WEIGHT = 0.4  # hard ceiling, whatever the yaml says
MAX_TOKENS = 128
_WHAT = {"name": "name", "description": "description", "x": "linked tweet"}


class Encoder(Protocol):
    """Anything that turns texts into one vector each (rows need not be normalised)."""

    def encode(self, texts: list[str]) -> np.ndarray: ...


@dataclass(frozen=True)
class EmbedConfig:
    prompts: dict[str, list[str]]
    threshold: float
    min_margin: float
    weight_floor: float
    max_weight: float
    full_weight_at: float
    min_text_chars: int
    max_text_chars: int
    only_when_unresolved: bool
    context_labels: tuple[str, ...]


@lru_cache
def load_config() -> EmbedConfig:
    with (DATA_DIR / "embed_labels.yaml").open(encoding="utf-8") as f:
        raw = yaml.safe_load(f)
    known = category_labels()
    prompts = {str(lbl): [str(p) for p in ps] for lbl, ps in raw["prompts"].items() if ps}
    unknown = sorted(set(prompts) - known)
    assert not unknown, f"embed_labels.yaml names labels not in taxonomy.yaml: {unknown}"
    s = raw["scoring"]
    return EmbedConfig(
        prompts=prompts,
        threshold=float(s["threshold"]),
        min_margin=float(s.get("min_margin", 0.0)),
        weight_floor=float(s["weight_floor"]),
        max_weight=min(MAX_WEIGHT, float(s["max_weight"])),
        full_weight_at=float(s["full_weight_at"]),
        min_text_chars=int(s.get("min_text_chars", 3)),
        max_text_chars=int(s.get("max_text_chars", 512)),
        only_when_unresolved=bool(raw.get("only_when_unresolved", True)),
        context_labels=tuple(raw.get("context_labels", [])),
    )


# ----------------------------------------------------------------- classification
def _unit(m: np.ndarray) -> np.ndarray:
    m = np.asarray(m, dtype=np.float32)
    if m.ndim == 1:
        m = m[None, :]
    norms = np.linalg.norm(m, axis=1, keepdims=True)
    return m / np.where(norms == 0, 1.0, norms)


class Classifier:
    """Label prompts embedded once per encoder; then nearest-label lookup per text."""

    def __init__(self, encoder: Encoder, cfg: EmbedConfig) -> None:
        self.encoder = encoder
        self.cfg = cfg
        self._labels: list[str] = []
        texts: list[str] = []
        for lbl, ps in cfg.prompts.items():
            for p in ps:
                self._labels.append(lbl)
                texts.append(p)
        self._prompt_texts = texts
        self._prompts = _unit(encoder.encode(texts)) if texts else np.zeros((0, 1), np.float32)

    def nearest(self, text: str) -> tuple[str, str, float, float] | None:
        """(label, prompt, cosine, margin over the next-best label), or None."""
        if not self._labels:
            return None
        v = _unit(self.encoder.encode([text]))[0]
        if v.shape[0] != self._prompts.shape[1]:
            return None
        sims = self._prompts @ v
        best: dict[str, tuple[float, int]] = {}
        for i, (lbl, s) in enumerate(zip(self._labels, sims, strict=True)):
            if lbl not in best or s > best[lbl][0]:
                best[lbl] = (float(s), i)
        ranked = sorted(best.items(), key=lambda kv: -kv[1][0])
        lbl, (sim, idx) = ranked[0]
        margin = sim - ranked[1][1][0] if len(ranked) > 1 else 1.0
        return lbl, self._prompt_texts[idx], sim, margin

    def weight(self, sim: float) -> float:
        c = self.cfg
        span = max(1e-6, c.full_weight_at - c.threshold)
        frac = max(0.0, min(1.0, (sim - c.threshold) / span))
        return round(min(c.max_weight, c.weight_floor + (c.max_weight - c.weight_floor) * frac), 3)

    def evidence(self, texts: list[tuple[Where, str]]) -> list[Ev]:
        """One row per text whose nearest label clears the threshold and the margin."""
        c = self.cfg
        out: list[Ev] = []
        for where, text in texts:
            text = " ".join((text or "").split())[: c.max_text_chars]
            if len(text) < c.min_text_chars:
                continue
            try:
                hit = self.nearest(text)
            except Exception:  # noqa: BLE001
                continue  # a bad text or encoder hiccup costs this row, not the analysis
            if hit is None:
                continue
            lbl, prompt, sim, margin = hit
            if sim < c.threshold or margin < c.min_margin:
                continue
            out.append(
                Ev(
                    kind="embedding",
                    label=lbl,
                    weight=self.weight(sim),
                    detail=f"embedding guess: the {_WHAT.get(where, where)} is closest to "
                    f'"{prompt}" '
                    f"(cosine {sim:.2f})",
                    source="embed:minilm",
                    where=where,
                )
            )
        return out


def unresolved(category_scores: list[tuple[str, float]], cfg: EmbedConfig) -> bool:
    """True when no topical category scored (context labels like derivative/* do not count)."""
    for lbl, _ in category_scores:
        if not any(lbl == c or lbl.startswith(c + "/") for c in cfg.context_labels):
            return False
    return True


_classifiers: dict[int, Classifier] = {}
_cls_lock = threading.Lock()


def classifier_for(encoder: Encoder) -> Classifier | None:
    """The cached classifier for this encoder (prompt embeddings computed once)."""
    key = id(encoder)
    c = _classifiers.get(key)
    if c is not None and c.encoder is encoder:
        return c
    with _cls_lock:
        c = _classifiers.get(key)
        if c is None or c.encoder is not encoder:
            try:
                c = Classifier(encoder, load_config())
            except Exception:  # noqa: BLE001
                return None
            if len(_classifiers) > 8:
                _classifiers.clear()  # stale encoders (tests, a reloaded model)
            _classifiers[key] = c
    return c


def evidence(encoder: Encoder | None, texts: list[tuple[Where, str]]) -> list[Ev]:
    if encoder is None:
        return []
    c = classifier_for(encoder)
    return c.evidence(texts) if c is not None else []


def guesses(
    encoder: Encoder,
    category_scores: list[tuple[str, float]],
    name: str | None,
    description: str | None,
    tweet: str | None,
) -> list[Ev]:
    """The engine's hook: embedding guesses for the name, description and tweet, only when
    the rule layers left the token without a topical category (unless the yaml says always)."""
    cfg = load_config()
    if cfg.only_when_unresolved and not unresolved(category_scores, cfg):
        return []
    candidates: list[tuple[Where, str | None]] = [
        ("name", name),
        ("description", description),
        ("x", tweet),
    ]
    texts = [(where, t) for where, t in candidates if t]
    return evidence(encoder, texts)


# ----------------------------------------------------------------- tokenizer (BERT WordPiece)
def _is_punct(ch: str) -> bool:
    cp = ord(ch)
    if 33 <= cp <= 47 or 58 <= cp <= 64 or 91 <= cp <= 96 or 123 <= cp <= 126:
        return True
    return unicodedata.category(ch).startswith("P")


def _is_cjk(cp: int) -> bool:
    return (
        0x4E00 <= cp <= 0x9FFF
        or 0x3400 <= cp <= 0x4DBF
        or 0x20000 <= cp <= 0x2A6DF
        or 0xF900 <= cp <= 0xFAFF
        or 0x2F800 <= cp <= 0x2FA1F
    )


class WordPiece:
    """The uncased BERT tokenizer MiniLM uses, from its vocab.txt. Pure Python, no deps."""

    def __init__(self, vocab: dict[str, int]) -> None:
        self.vocab = vocab
        self.unk = vocab.get("[UNK]", 100)
        self.cls = vocab.get("[CLS]", 101)
        self.sep = vocab.get("[SEP]", 102)

    @classmethod
    def from_file(cls, path: Path) -> WordPiece:
        with path.open(encoding="utf-8") as f:
            return cls({line.rstrip("\n"): i for i, line in enumerate(f)})

    def _basic(self, text: str) -> list[str]:
        text = unicodedata.normalize("NFD", text.lower())
        out: list[str] = []
        cur: list[str] = []

        def flush() -> None:
            if cur:
                out.append("".join(cur))
                cur.clear()

        for ch in text:
            cat = unicodedata.category(ch)
            if cat == "Mn" or (cat in ("Cc", "Cf") and ch not in "\t\n\r"):
                continue  # accents and control characters
            if ch.isspace():
                flush()
            elif _is_punct(ch) or _is_cjk(ord(ch)):
                flush()
                out.append(ch)
            else:
                cur.append(ch)
        flush()
        return out

    def _wordpiece(self, word: str) -> list[int]:
        if len(word) > 100:
            return [self.unk]
        ids: list[int] = []
        start = 0
        while start < len(word):
            end = len(word)
            found = None
            while start < end:
                piece = word[start:end] if start == 0 else "##" + word[start:end]
                if piece in self.vocab:
                    found = self.vocab[piece]
                    break
                end -= 1
            if found is None:
                return [self.unk]
            ids.append(found)
            start = end
        return ids

    def encode(self, text: str, max_len: int = MAX_TOKENS) -> list[int]:
        ids: list[int] = []
        for w in self._basic(text):
            ids += self._wordpiece(w)
        return [self.cls, *ids[: max_len - 2], self.sep]


# ----------------------------------------------------------------- ONNX encoder
class OnnxEncoder:
    """Sentence encoder: token ids -> ONNX model -> mean pooling over the attention mask."""

    def __init__(self, model_path: Path, vocab_path: Path) -> None:
        import onnxruntime as ort  # only reached with ENABLE_EMBED on

        opts = ort.SessionOptions()
        opts.intra_op_num_threads = 1
        opts.inter_op_num_threads = 1
        self.session = ort.InferenceSession(
            str(model_path), opts, providers=["CPUExecutionProvider"]
        )
        self.inputs = {i.name for i in self.session.get_inputs()}
        self.outputs = [o.name for o in self.session.get_outputs()]
        self.tok = WordPiece.from_file(vocab_path)

    def encode(self, texts: list[str]) -> np.ndarray:
        rows = [self.tok.encode(t) for t in texts]
        width = max(len(r) for r in rows)
        ids = np.zeros((len(rows), width), dtype=np.int64)
        mask = np.zeros_like(ids)
        for i, r in enumerate(rows):
            ids[i, : len(r)] = r
            mask[i, : len(r)] = 1
        feed: dict[str, Any] = {"input_ids": ids, "attention_mask": mask}
        if "token_type_ids" in self.inputs:
            feed["token_type_ids"] = np.zeros_like(ids)
        feed = {k: v for k, v in feed.items() if k in self.inputs}
        if "sentence_embedding" in self.outputs:
            (out,) = self.session.run(["sentence_embedding"], feed)
            return np.asarray(out, dtype=np.float32)
        (hidden,) = self.session.run([self.outputs[0]], feed)
        hidden = np.asarray(hidden, dtype=np.float32)
        if hidden.ndim == 2:  # already pooled
            return hidden
        m = mask[:, :, None].astype(np.float32)
        return (hidden * m).sum(axis=1) / np.maximum(m.sum(axis=1), 1e-9)


_enc_lock = threading.Lock()
_encoder: Encoder | None = None
_load_error: str | None = None
_loaded_from: str | None = None


def resolve_paths(model_path: str, vocab_path: str = "") -> tuple[Path, Path]:
    """EMBED_MODEL_PATH may be the .onnx file or a directory holding model.onnx (or
    onnx/model.onnx, as Hugging Face exports lay it out) and vocab.txt."""
    p = Path(model_path)
    if p.is_dir():
        model = next(
            (c for c in (p / "model.onnx", p / "onnx" / "model.onnx") if c.is_file()),
            p / "model.onnx",
        )
        base = p
    else:
        model = p
        base = p.parent
    if vocab_path:
        vocab = Path(vocab_path)
    else:
        vocab = next(
            (c for c in (base / "vocab.txt", base.parent / "vocab.txt") if c.is_file()),
            base / "vocab.txt",
        )
    return model, vocab


def default_encoder(settings: Settings) -> Encoder | None:
    """The process-wide encoder, or None when the flag is off or the model is unusable.
    Loaded once, lazily; a failed load is remembered (and logged) instead of retried."""
    global _encoder, _load_error, _loaded_from
    if not settings.enable_embed or not settings.embed_model_path:
        return None
    key = f"{settings.embed_model_path}|{settings.embed_vocab_path}"
    if _loaded_from == key:
        return _encoder
    with _enc_lock:
        if _loaded_from != key:
            _encoder, _load_error = None, None
            model, vocab = resolve_paths(settings.embed_model_path, settings.embed_vocab_path)
            try:
                if not model.is_file():
                    raise FileNotFoundError(f"no ONNX model at {model}")
                if not vocab.is_file():
                    raise FileNotFoundError(f"no vocab.txt at {vocab}")
                _encoder = OnnxEncoder(model, vocab)
            except Exception as e:  # noqa: BLE001
                _load_error = f"{type(e).__name__}: {e}"
                structlog.get_logger("embed").warning("embed_model_unavailable", error=_load_error)
            _loaded_from = key
    return _encoder


def load_error() -> str | None:
    return _load_error
