"""Ship one pod's logs to the validator until told to stop.

    async for batch in reader.stream():
        await self._post(batch)

That is the whole capture loop. The reader hands over batches and commits each one
once this side has placed it; termination is the validator's call, signalled by the
stop status code. A pod going away is a cancellation from the agent, not a protocol
event — the validator derives termination from its own state and is owed nothing on
the way out.

The shipper owns its asyncio task behind ``start()`` / ``stop()``, so the agent deals
in shippers rather than tasks.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import traceback
from typing import List, Optional

import aiohttp
from loguru import logger

from .checkpoint import CheckpointStore
from .config import LogShipperConfig
from .exceptions import (
    CaptureStopped,
    LogStreamingRejected,
    LogStreamingTerminated,
    PayloadTooLarge,
    TransientShipError,
)
from .models import ChutePod, LogBatch, LogLine
from .reader import PodLogReader

# HTTP 413: batch too large for the validator/proxy — split rather than retry.
_PAYLOAD_TOO_LARGE = 413


class PodLogShipper:
    """Captures one pod's logs for as long as the validator wants them."""

    def __init__(
        self,
        pod: ChutePod,
        config: LogShipperConfig,
        session: aiohttp.ClientSession,
        checkpoints: CheckpointStore,
    ):
        self._pod = pod
        self._config = config
        self._session = session
        self._url = config.logs_url(pod.config_id)
        self._reader = PodLogReader(pod, config, checkpoints)
        self._task: Optional[asyncio.Task] = None
        self._error: Optional[BaseException] = None

    # ── lifecycle ───────────────────────────────────────────────────────────

    def start(self) -> None:
        """Begin capturing in the background. Idempotent while already running."""
        if self.running:
            return
        self._error = None
        self._task = asyncio.create_task(
            self._run(), name=f"log-shipper:{self._pod.config_id}"
        )

    @property
    def running(self) -> bool:
        """True while capture is live.

        False once the validator has stopped us, so the agent knows not to start
        this pod again while it is still present.
        """
        return self._task is not None and not self._task.done()

    @property
    def error(self) -> Optional[BaseException]:
        """The unexpected exception that ended capture, if one did."""
        return self._error

    async def stop(self) -> None:
        """Cancel capture and wait for it to unwind.

        Anything shipped but not yet committed is simply not committed, so a later
        shipper re-sends it — at-least-once, deduped validator-side.
        """
        if self._task is None:
            return
        self._task.cancel()
        # Awaiting a cancelled task re-raises its CancelledError. That is the outcome
        # we asked for, not a failure, so it must not surface in the caller — the
        # agent stopping one departed pod would otherwise unwind its own poll loop.
        with contextlib.suppress(asyncio.CancelledError):
            await self._task
        self._task = None

    # ── capture ─────────────────────────────────────────────────────────────

    async def _run(self) -> None:
        """Consume batches until the validator stops us, or we are cancelled.

        The outer loop exists for transient failures only. A failed post must NOT be
        caught inside the ``async for`` body: completing that body normally hands
        control back to the generator, which commits — and would commit a batch that
        was never accepted. Letting it propagate tears the stream down without
        committing, and the next stream re-reads from the last commit.
        """
        logger.info(
            f"Capturing logs for chute pod {self._pod.name} "
            f"(config_id={self._pod.config_id})"
        )
        try:
            while True:
                try:
                    async for batch in self._reader.stream():
                        await self._post(batch)
                    return  # the stream ended: nothing left to read for this pod
                except TransientShipError:
                    logger.warning(
                        f"Ship failed for config_id={self._pod.config_id}; "
                        f"retrying from the last commit"
                    )
                    await asyncio.sleep(self._config.poll_interval_seconds)
        except CaptureStopped as stop:
            logger.info(
                f"Capture ended for config_id={self._pod.config_id}: {stop.reason}"
            )
        except asyncio.CancelledError:
            logger.info(f"Capture cancelled for config_id={self._pod.config_id}")
            raise
        except Exception as exc:  # noqa: BLE001 - the task is nobody else's to await
            self._error = exc
            logger.error(
                f"Capture failed for config_id={self._pod.config_id}: "
                f"{_failure_detail(exc)}"
            )

    # ── delivery ────────────────────────────────────────────────────────────

    async def _post(self, batch: List[LogLine]) -> None:
        """Deliver one batch, halving it if the validator says it is too large.

        Splitting recurses rather than adapting a stored batch size: the ceiling is
        configuration, and a guest we build has no business tuning it at runtime.
        Nothing commits until every half has landed, so a failure part-way through
        re-reads the whole batch.
        """
        try:
            await self._send(batch)
        except PayloadTooLarge:
            if len(batch) == 1:
                # One line the server will never take. Drop it rather than wedge the
                # pod; max_line_bytes already bounds what we send.
                logger.warning(
                    f"Dropping a single log line the validator refuses as too large "
                    f"for config_id={self._pod.config_id}"
                )
                return
            mid = len(batch) // 2
            await self._post(batch[:mid])
            await self._post(batch[mid:])

    async def _send(self, batch: List[LogLine]) -> None:
        """One POST, with retry and backoff.

        Returns when the validator accepts the batch. Raises LogStreamingTerminated
        (stop code), LogStreamingRejected (terminal), PayloadTooLarge (caller splits),
        or TransientShipError once the attempts are spent.
        """
        body = LogBatch(deployment_id=self._pod.deployment_id, logs=batch).model_dump()
        timeout = aiohttp.ClientTimeout(total=self._config.request_timeout_seconds)

        for attempt in range(self._config.retry_max_attempts):
            try:
                async with self._session.post(
                    self._url, json=body, timeout=timeout
                ) as response:
                    status = response.status
                    if status == self._config.stop_status_code:
                        raise LogStreamingTerminated()
                    if status in self._config.terminal_status_codes:
                        raise LogStreamingRejected(status, _rejection_reason(status))
                    if status == _PAYLOAD_TOO_LARGE:
                        raise PayloadTooLarge()  # retrying as-is cannot help
                    if 200 <= status < 300:
                        return
                    logger.warning(
                        f"Log ship for config_id={self._pod.config_id} "
                        f"got status {status}"
                    )
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                logger.warning(
                    f"Log ship for config_id={self._pod.config_id} failed: {exc}"
                )
            if attempt + 1 < self._config.retry_max_attempts:
                await asyncio.sleep(self._backoff(attempt))
        raise TransientShipError()

    def _backoff(self, attempt: int) -> float:
        delay = self._config.retry_base_delay_seconds * (2**attempt)
        return min(delay, self._config.retry_max_delay_seconds)


def _failure_detail(exc: BaseException) -> str:
    """Where an exception came from, never what it said.

    This service's journal is miner-readable (`SERVICE_ALLOWLIST`), and the data it
    handles is tenant log content. An exception MESSAGE can quote the value that
    caused it — pydantic renders `input_value=`, `JSONDecodeError` carries a document
    snippet, `UnicodeDecodeError` carries the offending bytes — so a broad handler
    that formats `exc` can publish a chute's output to the operator it is kept from.
    The hardened sink stops loguru rendering locals, but not this; `logger.exception`
    would reprint the message in the traceback's last line even without the f-string.

    Type plus the failing frame localises the fault without quoting the data. The
    exception object itself stays on `self._error` for in-process use.
    """
    frames = traceback.extract_tb(exc.__traceback__)
    if not frames:
        return type(exc).__name__
    last = frames[-1]
    return f"{type(exc).__name__} at {os.path.basename(last.filename)}:{last.lineno}"


def _rejection_reason(status: int) -> str:
    if status == 404:
        return "unknown config_id"
    if status == 403:
        return "cert/ownership rejected"
    return "rejected"
