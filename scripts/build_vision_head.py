"""Build the vision head (data/vision_head.npz + thresholds in data/vision_labels.yaml) and
report how well it does on held-out logos.

    uv run --with tokenizers python scripts/build_vision_head.py MODEL_DIR [--cache DIR] [--report]

MODEL_DIR holds a Hugging Face ONNX export of SigLIP base/16-224 (Xenova/siglip-base-patch16-224:
onnx/vision_model_quantized.onnx, onnx/text_model.onnx and tokenizer.json). The text model and
the tokenizer are only needed here: the prompt embeddings are stored in the head, so the
service ships the image tower alone.

Steps: fetch each labelled logo (tests/golden/vision_logos.yaml) into the cache, embed it with
the same preprocessing the service uses (engine/vision.py), embed every class's prompts (three
phrasings each, averaged), then
- --report: fit on the tune split, pick cutoffs out-of-fold on it, and print precision and
  recall per taxonomy label on the holdout split (nothing written);
- otherwise: fit on every logo, pick cutoffs out-of-fold, write the head and the cutoffs.
"""

from __future__ import annotations

import argparse
import hashlib
import re
import sys
from collections import defaultdict
from pathlib import Path

import httpx
import numpy as np
import yaml

from tokensage.engine import vision
from tokensage.taxonomy import DATA_DIR

ROOT = Path(__file__).resolve().parents[1]
LOGOS = ROOT / "tests" / "golden" / "vision_logos.yaml"
TEMPLATES = ("{}", "a token logo showing {}", "an image of {}")
L2 = 0.01  # probe ridge (0.001-0.03 all within a point on the tune split)
TARGET_PRECISION = 0.9
MIN_SUPPORT = 3
FOLDS = 5


def fetch(rows: list[dict], cache: Path) -> dict[str, bytes]:
    cache.mkdir(parents=True, exist_ok=True)
    out: dict[str, bytes] = {}
    client = httpx.Client(timeout=25, follow_redirects=True, headers={"user-agent": "Mozilla/5.0"})
    for r in rows:
        p = cache / r["mint"]
        if not p.is_file():
            url = r["image"]
            m = re.search(r"/ipfs/([A-Za-z0-9]+)", url or "")
            if m:  # public gateways rate-limit; pump.fun's own gateway does not
                url = f"https://pump.mypinata.cloud/ipfs/{m.group(1)}"
            try:
                resp = client.get(url)
                resp.raise_for_status()
                p.write_bytes(resp.content[:15_000_000])
            except Exception as e:  # noqa: BLE001
                print(f"skip {r['mint']}: {type(e).__name__}", file=sys.stderr)
                continue
        out[r["mint"]] = p.read_bytes()
    return out


def image_vectors(
    model_dir: Path, rows: list[dict], data: dict[str, bytes], cache: Path
) -> np.ndarray:
    path = vision.resolve_path(str(model_dir))
    tag = hashlib.sha256(path.read_bytes()).hexdigest()[:12]
    keyfile = cache / f"emb-{tag}.npz"
    have: dict[str, np.ndarray] = {}
    if keyfile.is_file():
        with np.load(keyfile) as z:
            have = dict(zip((str(m) for m in z["mints"]), z["vecs"], strict=True))
    enc = vision.OnnxEncoder(path, threads=4)
    for r in rows:
        if r["mint"] not in have and r["mint"] in data:
            have[r["mint"]] = enc.encode(vision.preprocess(data[r["mint"]]))
    np.savez(keyfile, mints=np.array(list(have)), vecs=np.stack(list(have.values())))
    return np.stack([have[r["mint"]] for r in rows])


def prompt_centroids(model_dir: Path, classes: dict) -> np.ndarray:
    import onnxruntime as ort
    from tokenizers import Tokenizer

    tok = Tokenizer.from_file(str(model_dir / "tokenizer.json"))
    sess = ort.InferenceSession(
        str(model_dir / "onnx" / "text_model.onnx"), providers=["CPUExecutionProvider"]
    )
    out = []
    for c in classes.values():
        vs = []
        for p in c["prompts"]:
            acc = []
            for t in TEMPLATES:
                ids = (tok.encode(t.format(p).lower()).ids + [1] * 64)[:64]  # pad id 1, length 64
                (v,) = sess.run(["pooler_output"], {"input_ids": np.array([ids], np.int64)})
                acc.append(v[0] / np.linalg.norm(v[0]))
            v = np.mean(acc, axis=0)
            vs.append(v / np.linalg.norm(v))
        m = np.mean(vs, axis=0)
        out.append(m / np.linalg.norm(m))
    return np.array(out, np.float32)


def fit_probe(x: np.ndarray, y: np.ndarray, scale: float, epochs: int = 400, lr: float = 0.5):
    w = np.zeros((x.shape[1], y.shape[1]), np.float32)
    b = np.zeros(y.shape[1], np.float32)
    xs = x * scale
    for _ in range(epochs):
        z = xs @ w + b
        z -= z.max(1, keepdims=True)
        p = np.exp(z)
        p /= p.sum(1, keepdims=True)
        g = p - y
        w -= lr * (xs.T @ g / len(xs) + L2 * w)
        b -= lr * g.mean(0)
    return w, b


def score_all(x: np.ndarray, head: vision.Head, cfg: vision.VisionConfig) -> np.ndarray:
    return np.stack([vision.scores(v, head, cfg) for v in x])


def oof_scores(x, y, groups, mask, centroids, names, cfg) -> np.ndarray:
    """Scores for the masked logos from probes that never saw their duplicate group."""
    out = np.zeros((len(x), len(names)), np.float32)
    g = np.unique(groups[mask])
    np.random.default_rng(3).shuffle(g)
    for fold in np.array_split(g, FOLDS):
        test = mask & np.isin(groups, fold)
        train = mask & ~test
        w, b = fit_probe(x[train], y[train], cfg.probe_scale)
        out[test] = score_all(x[test], vision.Head(names, centroids, w, b), cfg)
    return out


def cutoffs(
    sc: np.ndarray, truth: list[set], names: list[str], cfg, target: float = TARGET_PRECISION
) -> dict[str, float]:
    top = sc.argmax(1)
    thr: dict[str, float] = {}
    for k, c in enumerate(names):
        if not cfg.classes.get(c):
            continue
        hits = sorted(((sc[i, k], c in truth[i]) for i in np.where(top == k)[0]), reverse=True)
        tp = fp = 0
        for s, ok in hits:
            tp += ok
            fp += not ok
            if tp >= MIN_SUPPORT and tp / (tp + fp) >= target:
                thr[c] = round(float(s), 3)
    return thr


def report(sc, truth, mask, names, thr, cfg) -> None:
    per: dict[str, list[int]] = defaultdict(lambda: [0, 0, 0])  # tp, fp, relevant
    for i in np.where(mask)[0]:
        for c in truth[i]:
            if cfg.classes.get(c):
                per[cfg.classes[c]][2] += 1
        res = vision.VisionResult(
            cfg.model, [(names[k], float(sc[i, k])) for k in np.argsort(-sc[i])[:3]]
        )
        for ev in vision.evidence(res, cfg):
            ok = any(cfg.classes.get(c) == ev.label for c in truth[i])
            per[ev.label][0 if ok else 1] += 1
    tot = [sum(v[j] for v in per.values()) for j in range(3)]
    relevant = sum(1 for i in np.where(mask)[0] if any(cfg.classes.get(c) for c in truth[i]))
    head = ("label", "emitted", "right", "precision", "logos", "recall")
    print(f"{head[0]:34s} " + " ".join(f"{h:>9s}" for h in head[1:]))
    for lbl, (tp, fp, rel) in sorted(per.items(), key=lambda kv: -kv[1][2]):
        prec, rec = tp / max(1, tp + fp), tp / max(1, rel)
        print(f"{lbl:34s} {tp + fp:9d} {tp:9d} {prec:9.2f} {rel:9d} {rec:9.2f}")
    prec, rec = tot[0] / max(1, tot[0] + tot[1]), tot[0] / max(1, relevant)
    print(
        f"{'all':34s} {tot[0] + tot[1]:9d} {tot[0]:9d} {prec:9.2f} {relevant:9d} {rec:9.2f}"
        f"  (holdout logos: {int(mask.sum())})"
    )


def write_thresholds(thr: dict[str, float]) -> None:
    p = DATA_DIR / "vision_labels.yaml"
    text = p.read_text("utf-8")
    block = "thresholds:\n" + "".join(f"  {c}: {t}\n" for c, t in sorted(thr.items()))
    text = re.sub(r"^thresholds:.*\Z", block, text, flags=re.S | re.M)
    p.write_text(text, "utf-8")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("model_dir", type=Path)
    ap.add_argument("--cache", type=Path, default=ROOT / ".cache" / "vision_logos")
    ap.add_argument("--report", action="store_true")
    ap.add_argument("--precision", type=float, default=TARGET_PRECISION)
    a = ap.parse_args()

    raw = yaml.safe_load((DATA_DIR / "vision_labels.yaml").read_text("utf-8"))
    cfg = vision.load_config()
    names = list(raw["classes"])
    rows = yaml.safe_load(LOGOS.read_text("utf-8"))["logos"]
    data = fetch(rows, a.cache)
    rows = [r for r in rows if r["mint"] in data]
    unknown = {c for r in rows for c in r["labels"]} - set(names)
    assert not unknown, f"labels without a class in vision_labels.yaml: {unknown}"

    x = image_vectors(a.model_dir, rows, data, a.cache)
    x = x / np.linalg.norm(x, axis=1, keepdims=True)
    centroids = prompt_centroids(a.model_dir, raw["classes"])
    truth = [set(r["labels"]) for r in rows]
    y = np.zeros((len(rows), len(names)), np.float32)
    for i, t in enumerate(truth):
        for c in t:
            y[i, names.index(c)] = 1 / len(t)
    groups = np.array([r["group"] for r in rows])
    tune = np.array([r["split"] == "tune" for r in rows])
    hold = ~tune

    if a.report:
        thr = cutoffs(
            oof_scores(x, y, groups, tune, centroids, names, cfg), truth, names, cfg, a.precision
        )
        w, b = fit_probe(x[tune], y[tune], cfg.probe_scale)
        sc = score_all(x, vision.Head(names, centroids, w, b), cfg)
        cfg_t = vision.VisionConfig(**{**cfg.__dict__, "thresholds": thr})
        top1 = np.mean([names[sc[i].argmax()] in truth[i] for i in np.where(hold)[0]])
        print(f"tune {int(tune.sum())} logos, holdout {int(hold.sum())}; holdout top-1 {top1:.2f}")
        print("cutoffs (tune, out-of-fold):", thr)
        report(sc, truth, hold, names, thr, cfg_t)
        return

    everything = np.ones(len(rows), bool)
    thr = cutoffs(
        oof_scores(x, y, groups, everything, centroids, names, cfg), truth, names, cfg, a.precision
    )
    w, b = fit_probe(x, y, cfg.probe_scale)
    np.savez_compressed(
        DATA_DIR / "vision_head.npz",
        classes=np.array(names),
        centroids=centroids.astype(np.float32),
        w=w.astype(np.float32),
        b=b.astype(np.float32),
    )
    write_thresholds(thr)
    print(f"wrote vision_head.npz ({len(rows)} logos, {len(names)} classes); cutoffs {thr}")


if __name__ == "__main__":
    main()
