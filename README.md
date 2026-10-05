# TokenSage

An HTTP API that takes a Solana pump.fun token's **Contract Address (CA)** and explains what the token *means*: its name, ticker, image, description and linked X/Twitter content, with categories, flags, confidence scores and evidence. Built for other applications to call. No external AI APIs; deployed on Render via a Blueprint.

**Status:** research and planning. Start with [`PROJECT_GUIDE.md`](PROJECT_GUIDE.md).

- `docs/research/`: detailed research reports (pump.fun data, X access, non-AI understanding techniques, Render)
- `docs/reference/`: small, tested reference implementations to port into the codebase (`cd docs/reference && python -m pytest -q`)
