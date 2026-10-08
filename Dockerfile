# syntax=docker/dockerfile:1
FROM python:3.12-slim

# libgl1/libglib2.0-0 are needed by opencv-headless when RapidOCR lands (Phase 4).
RUN apt-get update && apt-get install -y --no-install-recommends libgl1 libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:0.10 /uv /usr/local/bin/uv

WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy PYTHONUNBUFFERED=1

# The logo-label model (ENABLE_CLIP, engine/vision.py): SigLIP base/16 image tower, 8-bit ONNX,
# Apache-2.0, ~100 MB. Pinned by revision and checked by hash; build with
# --build-arg VISION_MODEL=0 to leave it out (the analysis then runs without logo labels).
ARG VISION_MODEL=1
ARG VISION_MODEL_URL=https://huggingface.co/Xenova/siglip-base-patch16-224/resolve/4649052661e53c7000355844105f8a1792088239/onnx/vision_model_quantized.onnx
ARG VISION_MODEL_SHA256=ef14a954f3d57e1806666432bd9785004c1dc27100aa260eee0cb0f10a5de058
RUN if [ "$VISION_MODEL" = "1" ]; then \
      mkdir -p /app/models/siglip && python -c "import hashlib, sys, urllib.request; \
url, want, out = sys.argv[1:4]; data = urllib.request.urlopen(url, timeout=120).read(); \
got = hashlib.sha256(data).hexdigest(); \
sys.exit(f'vision model hash {got} != {want}') if got != want else open(out, 'wb').write(data)" \
        "$VISION_MODEL_URL" "$VISION_MODEL_SHA256" /app/models/siglip/vision_model_quantized.onnx; \
    fi

COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv uv sync --frozen --no-dev --no-install-project

COPY . .
RUN --mount=type=cache,target=/root/.cache/uv uv sync --frozen --no-dev

ENV PATH="/app/.venv/bin:$PATH"

# One entrypoint for every service; TOKENSAGE_ROLE picks api | worker | knowledge | maintenance.
# Exec form with an absolute path: no shell, no PATH lookup, and SIGTERM reaches Python directly.
CMD ["/app/.venv/bin/python", "-m", "tokensage.run"]
