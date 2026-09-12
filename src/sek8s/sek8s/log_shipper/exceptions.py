"""Exceptions for the chute log shipper — including capture control flow.

Capture of a single pod ends via a typed exception rather than sentinel return
values threaded up the call stack: the batch-post layer raises, and
``PodLogShipper.run()`` catches, so the stop reason is self-documenting.
"""

from __future__ import annotations


class LogShipperError(Exception):
    """Base class for all chute-log-shipper errors."""


class CrictlError(LogShipperError):
    """The crictl wrapper failed or returned unparseable output."""


class CaptureStopped(LogShipperError):
    """Base signal that capture of a pod should end. ``reason`` is for logging.

    Two concrete reasons, deliberately distinct: the validator *deliberately
    ended* streaming (LogStreamingTerminated) versus it *refused to accept* the
    shipment (LogStreamingRejected).
    """

    reason = "stopped"


class LogStreamingTerminated(CaptureStopped):
    """Validator deliberately ended log streaming for this pod (HTTP 204).

    The expected, non-error end of a successful capture: the chute activated /
    registered, so its pre-registration logs are no longer needed (default
    policy stops at activation; a per-chute override can extend this). We stop
    because the validator said we are done — not because anything failed.
    """

    reason = "validator ended streaming"


class LogStreamingRejected(CaptureStopped):
    """Validator refused to accept the shipment (HTTP 403/404).

    An error/authorization block, not a clean end: 404 = unknown config_id,
    403 = cert/ownership mismatch (cross-miner or misconfigured). Distinct from
    a transient failure — retrying can never succeed — so capture stops and
    logs ``reason``.
    """

    def __init__(self, status: int, reason: str):
        self.status = status
        self.reason = reason
        super().__init__(f"{status}: {reason}")


class TransientShipError(LogShipperError):
    """A batch could not be shipped after retries; retry on the next poll.

    Not a stop condition — the offset is left untouched so the same lines are
    re-read and re-sent next time around.
    """


class PayloadTooLarge(LogShipperError):
    """The validator/proxy rejected the batch as too large (HTTP 413).

    Not transient (retrying the same batch would 413 again) and not terminal —
    the shipper splits the batch and retries the halves, and shrinks its batch
    size ceiling so subsequent batches are pre-split.
    """
