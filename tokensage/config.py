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

    # --- copycats ---
    # A coin is flagged as a copycat only when the coin it copies (same name, ticker or logo)
    # launched within this many days before it; older namesakes are references, not copies.
    copycat_window_days: int = 30

    # --- behaviour flags ---
    accept_non_pump: bool = True
    enable_clip: bool = False
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
    # Creator-fee recipients (rules 0.19.0): a GitHub-linked recipient's numeric user id is
    # turned into a login via api.github.com (60 calls/h unauthenticated per IP; a token
    # allows 5,000/h). Results are cached in fee_recipient for fee_recipient_cache_days.
    enable_github_lookup: bool = True
    github_token: str = ""
    fee_recipient_cache_days: int = 7
    twitterapi_io_key: str = ""
    ipfs_gateways: str = (
        "https://pump.mypinata.cloud,https://dweb.link,https://ipfs.io,https://gateway.pinata.cloud"
    )
    fetch_connect_timeout_s: float = 5.0
    fetch_total_timeout_s: float = 10.0
    metadata_max_bytes: int = 64 * 1024
    image_max_bytes: int = 5 * 1024 * 1024

    # --- worker ---
    worker_poll_interval_s: float = 2.0
    # Concurrent claim/process loops in the analyzer worker. Cold analyses wait mostly on
    # IPFS/X/RPC, so several in flight multiply throughput. Each loop holds one DB connection.
    worker_concurrency: int = 8
    # On SIGTERM, in-flight jobs get this long to finish before they are handed back to the
    # queue (Render waits maxShutdownDelaySeconds=60 before killing the process).
    worker_shutdown_grace_s: float = 45.0
    # Loops when the analyzer runs inside the API process (INLINE_ANALYZER=true).
    inline_worker_concurrency: int = 2
    # OCR is CPU and memory heavy (RapidOCR ~250 MB); at most this many run at once.
    ocr_concurrency: int = 1
    job_lease_s: int = 120
    job_max_attempts: int = 3

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
    def ipfs_gateway_list(self) -> list[str]:
        return [g.strip().rstrip("/") for g in self.ipfs_gateways.split(",") if g.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
