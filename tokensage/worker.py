"""Analyzer worker: claims jobs from Postgres, runs the analyzer, reports back.

Run with `python -m tokensage.worker`. Also importable: the API runs `run_worker` as a
background task when INLINE_ANALYZER=true.
"""

from __future__ import annotations

import asyncio
import signal
import traceback

import asyncpg
import structlog

from tokensage import queue
from tokensage.analyzer import AnalyzeFailed, Context, analyze, retry_metadata
from tokensage.config import Settings, get_settings
from tokensage.db import create_pool
from tokensage.logging import configure_logging

log = structlog.get_logger("worker")


class Worker:
    def __init__(self, pool: asyncpg.Pool, settings: Settings):
        self.pool = pool
        self.settings = settings
        self.stop = asyncio.Event()
        self._wake = asyncio.Event()
        self._current_job_id: int | None = None
        self._listen_conn: asyncpg.Connection | None = None
        self.ctx = Context.create(settings)

    # -- lifecycle ---------------------------------------------------------

    async def run(self) -> None:
        await self._start_listener()
        log.info("worker.start")
        try:
            while not self.stop.is_set():
                did_work = await self._tick()
                if did_work:
                    continue
                self._wake.clear()
                try:
                    await asyncio.wait_for(
                        self._wait_for_wake(), timeout=self.settings.worker_poll_interval_s
                    )
                except TimeoutError:
                    pass
        finally:
            await self._stop_listener()
            await self.ctx.close()
            log.info("worker.stop")

    async def _wait_for_wake(self) -> None:
        stop_task = asyncio.create_task(self.stop.wait())
        wake_task = asyncio.create_task(self._wake.wait())
        try:
            await asyncio.wait({stop_task, wake_task}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for t in (stop_task, wake_task):
                t.cancel()

    async def _start_listener(self) -> None:
        self._listen_conn = await self.pool.acquire()
        await self._listen_conn.add_listener(queue.CHANNEL_NEW, self._on_new)

    async def _stop_listener(self) -> None:
        if self._listen_conn is not None:
            try:
                await self._listen_conn.remove_listener(queue.CHANNEL_NEW, self._on_new)
            finally:
                await self.pool.release(self._listen_conn)
                self._listen_conn = None

    def _on_new(self, *_args: object) -> None:
        self._wake.set()

    def request_stop(self) -> None:
        self.stop.set()
        self._wake.set()

    # -- work --------------------------------------------------------------

    async def _tick(self) -> bool:
        async with self.pool.acquire() as conn:
            job = await queue.claim(conn, self.settings.job_lease_s)
            if job is None:
                return False
            self._current_job_id = job.id
            try:
                await self._process(conn, job)
            finally:
                self._current_job_id = None
            return True

    async def _process(self, conn: asyncpg.Connection, job: queue.Job) -> None:
        bound = log.bind(job_id=job.id, kind=job.kind, mint=job.mint, depth=job.depth)
        if self.stop.is_set():
            await queue.release(conn, job.id)
            return
        try:
            if job.kind == "analyze":
                assert job.mint and job.depth
                version = await analyze(conn, self.ctx, job.mint, job.depth)
                await queue.complete(conn, job.id, version)
                bound.info("job.done", version=version)
            elif job.kind == "retry_metadata":
                assert job.mint and job.depth
                rv = await retry_metadata(conn, self.ctx, job.mint, job.depth)
                await queue.complete(conn, job.id, rv)
                bound.info("job.retry_metadata.done", version=rv)
            else:
                await queue.fail(conn, job.id, f"unknown job kind {job.kind}", max_attempts=1)
                bound.warning("job.unknown_kind")
        except AnalyzeFailed as exc:
            bound.info("job.definitive_failure", code=exc.code, error=str(exc))
            await queue.fail(conn, job.id, str(exc), max_attempts=1, error_code=exc.code)
        except Exception as exc:  # noqa: BLE001 - a bad token must never kill the loop
            err = "".join(traceback.format_exception_only(type(exc), exc)).strip()
            bound.error("job.error", error=err, attempt=job.attempts)
            await queue.fail(conn, job.id, err, max_attempts=self.settings.job_max_attempts)


async def run_worker(
    pool: asyncpg.Pool, settings: Settings, stop: asyncio.Event | None = None
) -> None:
    w = Worker(pool, settings)
    if stop is not None:

        async def _relay() -> None:
            await stop.wait()
            w.request_stop()

        relay = asyncio.create_task(_relay())
        try:
            await w.run()
        finally:
            relay.cancel()
    else:
        await w.run()


async def main() -> None:
    settings = get_settings()
    configure_logging(settings.log_level)
    pool = await create_pool(settings.database_url, min_size=1, max_size=3)
    w = Worker(pool, settings)
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, w.request_stop)
    try:
        await w.run()
    finally:
        await pool.close()


if __name__ == "__main__":
    asyncio.run(main())
