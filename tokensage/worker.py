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
from tokensage.analyzer import (
    AnalyzeFailed,
    Context,
    RetryRescheduled,
    analyze,
    retry_metadata,
)
from tokensage.config import Settings, get_settings
from tokensage.db import create_pool
from tokensage.engine import ocr
from tokensage.logging import configure_logging
from tokensage.net import metrics

log = structlog.get_logger("worker")


class _Deferred(Exception):
    """A callback job whose target has not finished; it has been put back to wait."""


class Worker:
    def __init__(self, pool: asyncpg.Pool, settings: Settings, concurrency: int | None = None):
        self.pool = pool
        self.settings = settings
        self.concurrency = max(1, concurrency or settings.worker_concurrency)
        self.stop = asyncio.Event()
        self._wake = asyncio.Event()
        self.active_jobs: set[int] = set()
        self._listen_conn: asyncpg.Connection | None = None
        self.ctx = Context.create(settings)
        ocr.set_concurrency(settings.ocr_concurrency)

    # -- lifecycle ---------------------------------------------------------

    async def run(self) -> None:
        await self._start_listener()
        log.info("worker.start", concurrency=self.concurrency)
        try:
            # N independent claim/process loops: one slow IPFS/X fetch no longer holds up
            # every other coin. Each loop takes its own pool connection per job.
            loops = [asyncio.create_task(self._loop(slot)) for slot in range(self.concurrency)]
            watchdog = asyncio.create_task(self._shutdown_watchdog(loops))
            try:
                await asyncio.gather(*loops, return_exceptions=True)
            finally:
                watchdog.cancel()
        finally:
            await self._stop_listener()
            await self.ctx.close()
            log.info("worker.stop")

    async def _shutdown_watchdog(self, loops: list[asyncio.Task[None]]) -> None:
        """After a stop request, give in-flight jobs a grace period, then cancel them; each
        cancelled job is handed back to the queue (see _tick) instead of staying 'running'
        until its lease expires."""
        await self.stop.wait()
        await asyncio.sleep(self.settings.worker_shutdown_grace_s)
        for t in loops:
            t.cancel()

    async def _loop(self, slot: int) -> None:
        while not self.stop.is_set():
            # clear before claiming: a notify arriving during the claim must not be lost
            self._wake.clear()
            try:
                did_work = await self._tick()
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 - a DB blip must not stop every slot
                log.error("worker.slot_error", slot=slot, error=f"{type(e).__name__}: {e}")
                await asyncio.sleep(min(5.0, self.settings.worker_poll_interval_s))
                continue
            if did_work:
                continue
            try:
                await asyncio.wait_for(
                    self._wait_for_wake(), timeout=self.settings.worker_poll_interval_s
                )
            except TimeoutError:
                pass

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
            job = await queue.claim(
                conn, self.settings.job_lease_s, max_attempts=self.settings.job_max_attempts
            )
            if job is None:
                return False
            self.active_jobs.add(job.id)
            # The job runs as its own task so the heartbeat can stop it: once the lease is
            # lost (another slot, process or the maintenance requeue owns the job now) going
            # on would analyse the coin twice, and every status write would miss anyway.
            lost = asyncio.Event()
            work = asyncio.create_task(self._process(conn, job))
            heartbeat = asyncio.create_task(self._heartbeat(job, work, lost))
            try:
                await work
            except asyncio.CancelledError:
                if lost.is_set():
                    log.warning("worker.job_abandoned", job_id=job.id, kind=job.kind)
                    return True
                await self._release_quietly(job)
                raise
            finally:
                heartbeat.cancel()
                self.active_jobs.discard(job.id)
            return True

    async def _heartbeat(
        self, job: queue.Job, work: asyncio.Task[None], lost: asyncio.Event
    ) -> None:
        """Keep the lease alive while a (possibly slow, OCR-queued) job runs, so no other
        slot or the maintenance job re-claims it and analyses the coin twice. When the
        renewal says the lease is no longer ours, the job is stopped."""
        every = max(1.0, self.settings.job_lease_s / 3)
        while True:
            await asyncio.sleep(every)
            try:
                async with self.pool.acquire() as c:
                    ok = await queue.renew_lease(
                        c, job.id, self.settings.job_lease_s, job.lease_token
                    )
            except Exception as e:  # noqa: BLE001
                log.warning("worker.heartbeat_failed", job_id=job.id, error=str(e)[:120])
                continue
            if not ok:
                if work.done() or await self._finished(job):
                    return  # the job wrote its final status; nothing to abandon
                log.warning("worker.lease_lost", job_id=job.id, kind=job.kind)
                lost.set()
                work.cancel()
                return

    async def _finished(self, job: queue.Job) -> bool:
        """The renewal missed because the job just finished (its final status is written
        a moment before the notify and callback release), not because the lease was lost."""
        try:
            async with self.pool.acquire() as c:
                row = await queue.get(c, job.id)
        except Exception:  # noqa: BLE001
            return False
        return row is not None and row.status in ("done", "failed")

    async def _release_quietly(self, job: queue.Job) -> None:
        try:
            async with self.pool.acquire() as c:
                await queue.release(c, job.id, job.lease_token)
            log.info("worker.job_released", job_id=job.id)
        except Exception as e:  # noqa: BLE001
            log.warning("worker.release_failed", job_id=job.id, error=str(e)[:120])

    async def _callback(self, conn: asyncpg.Connection, job: queue.Job) -> None:
        from tokensage import callbacks
        from tokensage.api import service
        from tokensage.api.schemas import JobResponse, TokenResponse, stored_analysis

        payload = job.payload or {}
        target = await queue.get(conn, int(payload["target_job_id"]))
        if target is None:
            return
        if target.status not in ("done", "failed"):
            # Not finished yet: wait for it without spending a delivery attempt. Finishing
            # the target releases this job straight away.
            await queue.defer(conn, job.id, callbacks.CALLBACK_WAIT_S, job.lease_token)
            # the target may have finished between our read and the defer: if so, run again
            # now instead of waiting out CALLBACK_WAIT_S
            again = await queue.get(conn, target.id)
            if again is not None and again.status in ("done", "failed"):
                await conn.execute(
                    "update job set run_after = now() where id=$1 and status='pending'", job.id
                )
                await conn.execute("select pg_notify($1, '0')", queue.CHANNEL_NEW)
            raise _Deferred
        result = None
        if target.status == "done" and target.mint and target.depth:
            latest = await service.latest_analysis(conn, target.mint, target.depth)
            if latest:
                doc, _ = latest
                result = TokenResponse(
                    ca=target.mint,
                    status=service._status_for(doc),  # type: ignore[arg-type]
                    depth=doc["depth"],
                    analysis=stored_analysis(doc),
                    request_id=f"callback-{job.id}",
                )
        body = JobResponse(
            job_id=target.id,
            status=target.status,  # type: ignore[arg-type]
            ca=target.mint,
            depth=target.depth,  # type: ignore[arg-type]
            result=result,
            error=(f"{target.error_code}: " if target.error_code else "")
            + (target.last_error or "")
            if target.status == "failed"
            else None,
            request_id=f"callback-{job.id}",
        ).model_dump(mode="json")
        await callbacks.deliver(self.ctx.http, payload, body)

    async def _process(self, conn: asyncpg.Connection, job: queue.Job) -> None:
        bound = log.bind(job_id=job.id, kind=job.kind, mint=job.mint, depth=job.depth)
        if self.stop.is_set():
            await queue.release(conn, job.id, job.lease_token)
            return
        try:
            if job.kind == "analyze":
                assert job.mint and job.depth
                hints = (job.payload or {}).get("hints") if job.payload else None
                version = await analyze(conn, self.ctx, job.mint, job.depth, hints=hints)
                if await queue.complete(conn, job.id, version, job.lease_token):
                    bound.info("job.done", version=version)
                else:
                    bound.warning("job.done_after_lease_lost", version=version)
            elif job.kind == "callback":
                try:
                    await self._callback(conn, job)
                except _Deferred:
                    bound.info("job.callback.waiting_for_target")
                    return
                await queue.complete(conn, job.id, None, job.lease_token)
                bound.info("job.callback.done")
            elif job.kind == "retry_metadata":
                assert job.mint and job.depth
                try:
                    rv = await retry_metadata(conn, self.ctx, job.mint, job.depth, job_id=job.id)
                except RetryRescheduled:
                    bound.info("job.retry_metadata.rescheduled")
                    return
                await queue.complete(conn, job.id, rv, job.lease_token)
                bound.info("job.retry_metadata.done", version=rv)
            else:
                await queue.fail(
                    conn,
                    job.id,
                    f"unknown job kind {job.kind}",
                    max_attempts=1,
                    lease_token=job.lease_token,
                )
                bound.warning("job.unknown_kind")
        except AnalyzeFailed as exc:
            bound.info("job.definitive_failure", code=exc.code, error=str(exc))
            await queue.fail(
                conn,
                job.id,
                str(exc),
                max_attempts=1,
                error_code=exc.code,
                lease_token=job.lease_token,
            )
        except Exception as exc:  # noqa: BLE001 - a bad token must never kill the loop
            err = "".join(traceback.format_exception_only(type(exc), exc)).strip()
            bound.error("job.error", error=err, attempt=job.attempts)
            if job.kind == "callback":
                from tokensage import callbacks

                await queue.fail(
                    conn,
                    job.id,
                    err,
                    max_attempts=callbacks.CALLBACK_MAX_ATTEMPTS,
                    retry_in_s=callbacks.CALLBACK_RETRY_S,
                    lease_token=job.lease_token,
                )
            else:
                await queue.fail(
                    conn,
                    job.id,
                    err,
                    max_attempts=self.settings.job_max_attempts,
                    lease_token=job.lease_token,
                )


async def run_worker(
    pool: asyncpg.Pool,
    settings: Settings,
    stop: asyncio.Event | None = None,
    concurrency: int | None = None,
) -> None:
    w = Worker(pool, settings, concurrency=concurrency)
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
    concurrency = max(1, settings.worker_concurrency)
    # one connection per concurrent job, plus the LISTEN connection, the metrics flusher's
    # and a spare
    pool = await create_pool(settings.database_url, min_size=2, max_size=concurrency + 3)
    w = Worker(pool, settings, concurrency=concurrency)
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, w.request_stop)
    stop_flush = asyncio.Event()
    flusher = asyncio.create_task(metrics.run_flusher(pool, stop_flush))
    try:
        await w.run()
    finally:
        stop_flush.set()
        await flusher  # the last counts land before the pool closes
        await pool.close()


if __name__ == "__main__":
    asyncio.run(main())
