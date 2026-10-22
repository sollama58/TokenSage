"""Runtime configuration. Every knob lives here or in data/*.yaml, never in code."""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def normalize_database_url(url: str) -> str:
    """Render hands out `postgres://`; asyncpg and SQLAlchemy want `postgresql://`."""
    if url.startswith("postgres://"):
        return "postgresql://" + url[len("postgres://") :]
    return url


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # --- service ---
    log_level: str = "info"
    http_user_agent: str = "TokenSage/0.1 (+https://github.com/sollama58/TokenSage)"
    database_url: str = "postgresql://tokensage:tokensage@localhost:5432/tokensage"
    inline_analyzer: bool = (
        False  # run the analyzer loop inside the web process (dev / tiny traffic)
    )

    # --- auth ---
    # "name:key,name2:key2". Keys are compared by SHA-256 in constant time.
    api_keys: str = ""
    admin_key: str = ""
    # read-only key for the monitoring GETs under /admin/v1 (status, usage, recall, helius,
    # upstreams, queue, signals); the admin key works there too. Empty = off.
    monitor_key: str = ""
    # Key names (as in API_KEYS / the api_key table) whose hints are first-party data, e.g.
    # "trenchscanner". Another key's hint-derived read stays private to it (no shared cache
    # hit, x_ref or token_read rows); a trusted key's read is stored like a fetched one, so
    # X-reuse counts and launch waves keep seeing the coins it reads. Empty = trust none.
    trusted_hint_keys: str = ""
    rate_per_min_default: int = 60
    full_per_day_default: int = 2000
    refresh_per_day_default: int = 200

    # --- request flow ---
    default_depth: str = "full"
    default_wait_s: int = 10
    max_wait_s: int = 25
    max_queue_depth: int = 500  # above this the API answers 503 overloaded
    batch_max: int = 50

    # --- freshness (seconds) ---
    max_age_young_s: int = 300  # token < 1 h old
    max_age_mid_s: int = 3600  # 1 h .. 7 d
    max_age_old_s: int = 86400  # > 7 d

    # --- X engagement freshness (since rules 0.30.0) ---
    # For coins younger than X_ENGAGEMENT_YOUNG_S, the linked post (and its quoted / replied-to
    # posts) is fetched again when its stored copy is older than X_ENGAGEMENT_REFRESH_S, and
    # a cached read is not served once its x.engagement is older than that. Older coins keep
    # the cached copy (re-checked every 6 h for deletion). A failed re-fetch is retried at
    # most every X_ENGAGEMENT_RETRY_S, and a read this young is served whatever its as_of.
    x_engagement_refresh_s: int = 300
    x_engagement_young_s: int = 7200
    x_engagement_retry_s: int = 60

    # --- decision-time refresh, POST /v1/tokens:now (since rules 0.34.0) ---
    # A stored refresh (fresh trend block and story heat) is served while younger than this;
    # older, the request starts a new one.
    now_max_age_s: int = 300
    # `now` jobs a key may start per UTC day (joining an open one is free); past it the call
    # answers 429 quota_exceeded. TrenchScanner refreshes each band coin about every 4 min.
    now_per_day: int = 50_000

    # --- basic-depth X interpretation (since rules 0.36.0) ---
    # On depth=basic, use cached tweet/profile text when present (tier A), and optionally
    # fetch the primary post only (tier B; no quoted/replied/profile chain, media, or trends).
    enable_basic_x: bool = True
    enable_basic_x_fetch: bool = True  # false = cache-only tier A
    basic_x_timeout_s: float = 3.0

    # --- full-depth job deadline (since rules 0.30.0) ---
    # The full-depth fetches (X, OCR, logo labels, trends, Wikipedia, news, Bluesky) run
    # concurrently; whatever is unfinished after this many seconds is cancelled and the read
    # is returned as partial, naming the sources in `errors`.
    full_depth_deadline_s: float = 20.0

    # --- copycats ---
    # A coin is flagged as a copycat only when the coin it copies (same name, ticker or logo)
    # launched within this many days before it; older namesakes are references, not copies.
    copycat_window_days: int = 30

    # --- retention (the hourly maintenance cron, tokensage/jobs/maintenance.py) ---
    # Database audit 2026-10-16 (I1): nothing in the token family was ever pruned, and at
    # 36-49k reads a day of 4-6 KB each the 15 GB default disk had weeks left. Two horizons:
    # a token's heavy rows (analysis, token_metadata, token_read, token_market) go after
    # token_detail_retention_days, the slim token row itself (name, symbol, creator,
    # created_at, logo hash) and its x_ref after token_retention_days. The row is what the
    # current-meta baseline (data/meta.yaml history_days, 90) and the copycat window
    # (copycat_window_days, 30) count, so token_retention_days must stay at or above both.
    # Each mint keeps its newest analysis at each depth past the detail horizon: pair tokens
    # (resolve/pair.py) and lineage originals (analyzer._prior_reads, any age for top-volume
    # coins) are read back from it, and /v1/tokens:now needs the full read. A token with an
    # analysis newer than token_detail_retention_days (read recently) is exempt from the row
    # horizon, however old its launch. The other heavy rows are re-read on demand. The
    # caches keyed by something other than the mint (image, x_tweet, x_profile,
    # x_profile_history, x_media, pair_token, fee_recipient) go on the detail horizon too,
    # from their fetch time. 0 turns a horizon off.
    token_detail_retention_days: int = 30
    token_retention_days: int = 90
    # Every analysis is condensed once it is this old (TrenchScanner's pattern: a bug
    # report's screenshot goes after a week, the report stays): its evidence list and market
    # snapshot are dropped, and a basic read's x, image and trend blocks too, about 60% of
    # a stored read. What later reads use stays: raw, normalized, referent, categories,
    # lineage and copy_of (pair tokens, lineage originals), and a full read's x, image and
    # trend (/v1/tokens:now). A read this old is stale on every max_age, so a request re-runs
    # it. 0 turns it off.
    analysis_condense_days: int = 7

    # --- behaviour flags ---
    accept_non_pump: bool = True
    # Optional logo labels from a local vision model (engine/vision.py), full depth only:
    # a SigLIP image tower in ONNX on disk (the Docker image ships one at /app/models/siglip).
    enable_clip: bool = False
    # Logo OCR at full depth (engine/ocr.py). Off since 2026-10-16: OCR runs one image at a
    # time, so concurrent deep reads held analyzer slots while queued on it, and the logo-derived
    # inputs read no signal for TrenchScanner (AUC 0.49-0.55 on 3,886 graded decisions).
    # Cached OCR text is still used. ENABLE_OCR=true turns it back on.
    enable_ocr: bool = False
    vision_model_path: str = ""  # the .onnx file, or a directory holding vision_model*.onnx
    vision_threads: int = 2  # onnxruntime threads per image (one image at a time per process)
    # Optional local sentence-embedding classifier (engine/embed.py): guesses categories for
    # names and tweets nothing else resolves. Needs a MiniLM-class ONNX model on disk.
    enable_embed: bool = False
    embed_model_path: str = ""  # the .onnx file, or a directory holding model.onnx + vocab.txt
    embed_vocab_path: str = ""  # vocab.txt, when it is not next to the model
    enable_corpus: bool = False
    enable_paid_x: bool = False
    paid_x_daily_usd_cap: float = 1.0
    paid_x_usd_per_call: float = 0.00015  # twitterapi.io: about $0.15 per 1,000 tweets

    # DEV ONLY: lets the SSRF guard accept http:// / private hosts (local fake chain server).
    dev_allow_insecure_fetch: bool = False

    # --- upstream ---
    solana_rpc_url: str = ""
    solana_ws_url: str = ""
    # Helius plan, for the admin panel's credit gauge: monthly credits, the day of the month
    # the credits reset (the subscription date), and the plan's RPC requests per second.
    # Defaults are the Free plan; Developer is 10,000,000 credits and 50 req/s.
    helius_plan_credits: int = 1_000_000
    helius_billing_day: int = Field(default=1, ge=1, le=28)
    helius_rps_limit: int = 10
    # "method:credits,..." overrides for the credit table in net/metrics.py
    helius_credit_costs: str = ""
    coingecko_api_key: str = ""
    # Creator-fee recipients (rules 0.19.0): what a plain wallet or another program's account
    # is, cached in fee_recipient for fee_recipient_cache_days. A GitHub-linked recipient is
    # reported by its numeric account id; its login is not looked up (since rules 0.22.0).
    fee_recipient_cache_days: int = 7
    twitterapi_io_key: str = ""
    ipfs_gateways: str = (
        "https://pump.mypinata.cloud,https://dweb.link,https://ipfs.io,https://gateway.pinata.cloud"
    )
    fetch_connect_timeout_s: float = 5.0
    fetch_total_timeout_s: float = 10.0
    # IPFS: the next gateway starts this long after the previous one, and the whole race gives
    # up after ipfs_race_timeout_s. A CID no gateway has yet (a launch seconds old) used to hold
    # an analyzer slot ~14.5 s for the metadata and again for the image; the metadata retry
    # schedule (resolve/metadata.py RETRY_SCHEDULE_S) asks again later.
    # 0.5 s was about the first gateway's average answer (489 ms), so half the fetches also
    # started the public gateways, which throttle (audit 2026-10-17).
    ipfs_stagger_s: float = 1.0
    ipfs_race_timeout_s: float = 7.0
    metadata_max_bytes: int = 64 * 1024
    image_max_bytes: int = 5 * 1024 * 1024

    # --- worker ---
    worker_poll_interval_s: float = 2.0
    # Concurrent claim/process loops in the analyzer worker. Cold analyses wait mostly on
    # IPFS/X/RPC, so several in flight multiply throughput. Each loop holds one DB connection.
    worker_concurrency: int = 12
    # On SIGTERM, in-flight jobs get this long to finish before they are handed back to the
    # queue (Render waits maxShutdownDelaySeconds=60 before killing the process).
    worker_shutdown_grace_s: float = 45.0
    # Loops when the analyzer runs inside the API process (INLINE_ANALYZER=true).
    inline_worker_concurrency: int = 2
    # OCR is CPU and memory heavy (RapidOCR ~250 MB); at most this many run at once.
    ocr_concurrency: int = 1
    # Threads for CPU-bound work (tokensage/cpu.py), apart from the default executor that
    # serves DNS lookups for every fetch.
    cpu_threads: int = 3
    job_lease_s: int = 120
    job_max_attempts: int = 3
    # A low-priority launch read still waiting after this long is dropped (queue.expire_stale_low):
    # TrenchScanner stops asking after ~11 minutes, and a backlog of them filled the queue.
    batch_low_max_wait_s: float = 900.0

    # --- render platform ---
    port: int = Field(default=10000, alias="PORT")

    @field_validator("database_url")
    @classmethod
    def _norm_db(cls, v: str) -> str:
        return normalize_database_url(v)

    @property
    def api_key_pairs(self) -> dict[str, str]:
        """name -> raw key, parsed from API_KEYS."""
        out: dict[str, str] = {}
        for item in self.api_keys.split(","):
            item = item.strip()
            if not item:
                continue
            name, _, key = item.partition(":")
            if name and key:
                out[name.strip()] = key.strip()
        return out

    @property
    def trusted_hint_key_set(self) -> frozenset[str]:
        return frozenset(k.strip() for k in self.trusted_hint_keys.split(",") if k.strip())

    @property
    def ipfs_gateway_list(self) -> list[str]:
        return [g.strip().rstrip("/") for g in self.ipfs_gateways.split(",") if g.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
