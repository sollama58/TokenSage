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

    # --- behaviour flags ---
    accept_non_pump: bool = True
    enable_clip: bool = False
    enable_corpus: bool = False
    enable_paid_x: bool = False
    paid_x_daily_usd_cap: float = 1.0

    # DEV ONLY: lets the SSRF guard accept http:// / private hosts (local fake chain server).
    dev_allow_insecure_fetch: bool = False

    # --- upstream ---
    solana_rpc_url: str = ""
    solana_ws_url: str = ""
    coingecko_api_key: str = ""
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
