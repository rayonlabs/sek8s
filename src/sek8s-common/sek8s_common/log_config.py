"""Central loguru configuration for the guest services.

Loguru's default handler renders exception tracebacks with ``diagnose=True``,
which annotates each frame with the *values* of its local variables. Inside the
guest that is a confidentiality problem, not just noise: the guest journals are
readable by the miner over the system-status API
(``/status/services/{id}/logs``), and the miner is the party the confidential VM
protects its tenants' data from. Anywhere loguru handles an exception whose frames
hold request data — a pod spec, a chute log window — the default sink would write
those values straight into an allowlisted journal. (The admission path happens to
use stdlib ``logging``, which never renders locals; the guest mixes both libraries,
so the guarantee is set here rather than per call site.)

So every service entrypoint installs its own sink with ``diagnose=False``. That is
the *only* setting this module changes from loguru's defaults; everything else is
left alone deliberately, and two of those defaults are worth knowing:

- ``backtrace`` stays on (default ``True``). Only values are suppressed — the
  traceback is untouched, so an exception still logs every frame from the catch
  point down to the raise with file, line, and source line, plus the call chain
  above the catching point. It renders frames, never values, and that stack is
  the whole diagnostic story when ``journalctl`` through the status API is the
  only view of a prod guest.
- ``enqueue`` stays off (default ``False``). Enqueueing would move the sink write
  off the event loop, but queued records are lost if the process dies before the
  worker thread drains them — and this guest is built to die abruptly
  (``OnFailure=poweroff.target``, ``FailureAction=poweroff-force``) with the
  journal as its only forensic surface, so the last line before a shutdown is
  exactly the one worth keeping. Its multiprocess-safety benefit does not apply
  either: every service is a single uvicorn process (no ``workers``) or a single
  asyncio daemon. Revisit if that changes.
"""

from __future__ import annotations

import sys

from loguru import logger

DEFAULT_FORMAT = (
    "<green>{time:YYYY-MM-DD HH:mm:ss.SSS}</green> | "
    "<level>{level: <8}</level> | "
    "<cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> - "
    "<level>{message}</level>"
)


def configure_logging(debug: bool = False) -> None:
    """Replace loguru's default sink with a diagnose-free stderr sink.

    Call once from a service entrypoint, before anything is logged. ``debug``
    only lowers the level — variable rendering stays off in every build, since
    a debug *image* still ships the same journal-exposing status API.
    """
    logger.remove()
    logger.add(
        sys.stderr,
        level="DEBUG" if debug else "INFO",
        format=DEFAULT_FORMAT,
        diagnose=False,
    )
