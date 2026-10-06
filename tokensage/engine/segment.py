"""S2 Segmentation: split concatenated names with a meme-aware vocabulary (guide §5.2).

wordsegment (Apache-2.0, ~100 MB RSS) with injected unigrams. Loaded lazily, once.
"""

from __future__ import annotations

import threading

from tokensage.engine.knowledge import Knowledge

_lock = threading.Lock()
_loaded = False
_common: frozenset[str] | None = None
_segmenter = None

# Extra weight for domain words so they beat the English prior (research: "dog w if hat").
_BOOST = 5e8


def _ensure_loaded(k: Knowledge) -> None:
    global _loaded, _segmenter
    if _loaded:
        return
    with _lock:
        if _loaded:
            return
        from wordsegment import Segmenter

        seg = Segmenter()
        seg.load()
        global _common
        # everyday English by raw frequency, captured before domain words get boosted
        _common = frozenset(
            w for w, _ in sorted(seg.unigrams.items(), key=lambda kv: -kv[1])[:20000]
        )
        for w in k.vocabulary():
            seg.unigrams[w] = max(seg.unigrams.get(w, 0.0), _BOOST)
        # a few compounds we want kept whole
        for w in ("dogwifhat", "catwifhat", "chillguy", "moodeng", "pnut", "fwog", "michi"):
            seg.unigrams[w] = max(seg.unigrams.get(w, 0.0), _BOOST * 2)
        seg.total = sum(seg.unigrams.values())
        _segmenter = seg
        _loaded = True


def common_words(k: Knowledge) -> frozenset[str]:
    """The 20k most frequent everyday English words (raw frequency, before domain boosts)."""
    _ensure_loaded(k)
    assert _common is not None
    return _common


def segment_compact(compact: str, k: Knowledge) -> list[str]:
    if not compact:
        return []
    if not compact.isascii():
        return [compact]
    _ensure_loaded(k)
    assert _segmenter is not None
    try:
        return list(_segmenter.segment(compact))
    except Exception:  # noqa: BLE001
        return [compact]


def _score(tokens: list[str], k: Knowledge) -> float:
    """Prefer segmentations whose tokens hit the lexicon / gazetteers and aren't 1-letter."""
    vocab = _vocab(k)
    score = 0.0
    for t in tokens:
        if t in vocab:
            score += 2.0
        elif len(t) == 1 and not t.isdigit():
            score -= 1.5
        elif len(t) == 2 and t not in ("ai", "og", "cz", "gm", "gn", "xi", "ye"):
            score -= 0.4
    return score - 0.15 * len(tokens)


_vocab_cache: set[str] | None = None


def _vocab(k: Knowledge) -> set[str]:
    global _vocab_cache
    if _vocab_cache is None:
        v = set(k.vocabulary())
        for words in k.wordnet.values():
            v.update(w for w in words if " " not in w)
        _vocab_cache = v
    return _vocab_cache


def best_tokens(spaced: str, compact: str, k: Knowledge) -> list[str]:
    """Given the name as written (lowercased, folded) and its compact form, return the
    better token list. Multi-word names keep their spacing unless the segmenter finds a
    clearly better reading (e.g. 'elonsdog' inside a longer phrase)."""
    given = spaced.split()
    if not compact:
        return given
    resegmented = segment_compact(compact, k)
    if not given:
        return resegmented
    if len(given) >= 2:
        # try re-segmenting each over-long given token individually
        improved: list[str] = []
        for t in given:
            if len(t) >= 9 and t.isalpha():
                parts = segment_compact(t, k)
                improved.extend(parts if _score(parts, k) > _score([t], k) else [t])
            else:
                improved.append(t)
        return improved
    # single token: compare
    return resegmented if _score(resegmented, k) > _score(given, k) else given
