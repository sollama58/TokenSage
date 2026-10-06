"""Per-source circuit breaker, in-process, mirrored to source_health for /readyz."""

from __future__ import annotations

import time
from dataclasses import dataclass, field

import asyncpg


@dataclass
class _State:
    failures: int = 0
    open_until: float = 0.0


@dataclass
class CircuitBreaker:
    threshold: int = 5
    cooldown_s: float = 120.0
    states: dict[str, _State] = field(default_factory=dict)

    def allow(self, source: str) -> bool:
        st = self.states.get(source)
        return not st or st.open_until <= time.monotonic()

    MAX_SOURCES = 2000

    def success(self, source: str) -> None:
        # closed == absent: healthy hosts (attacker-chosen ones included) leave no entry
        self.states.pop(source, None)

    def failure(self, source: str) -> bool:
        """Record a failure; True if the breaker just opened."""
        if source not in self.states and len(self.states) >= self.MAX_SOURCES:
            now = time.monotonic()
            for k in [k for k, v in self.states.items() if v.open_until <= now]:
                del self.states[k]
        st = self.states.setdefault(source, _State())
        st.failures += 1
        if st.failures >= self.threshold and st.open_until <= time.monotonic():
            st.open_until = time.monotonic() + self.cooldown_s
            return True
        return False

    async def persist(self, conn: asyncpg.Connection) -> None:
        now = time.monotonic()
        for source, st in self.states.items():
            await conn.execute(
                """insert into source_health (source, state, failures, open_until)
                   values ($1, $2, $3, case when $4 > 0 then now() + make_interval(secs => $4) end)
                   on conflict (source) do update set state=excluded.state,
                     failures=excluded.failures, open_until=excluded.open_until""",
                source,
                "open" if st.open_until > now else "closed",
                st.failures,
                max(0.0, st.open_until - now),
            )


breaker = CircuitBreaker()
