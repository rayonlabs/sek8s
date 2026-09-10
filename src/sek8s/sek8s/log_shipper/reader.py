"""Turn a pod's CRI log files into complete log lines.

Everything file-shaped lives here so the shipper above can be a plain loop. Four
things make this harder than "read the file", and each is why a piece of state exists:

  ROTATION   kubelet renames the current file and opens a new one. Offsets are keyed
             by inode, so progress follows the rename instead of resetting.
  RESTART    The shipper is a service; on restart it must resume where it stopped.
             That is why progress is a persisted offset rather than an open handle.
  FRAMING    CRI splits a long line into N `P` chunks terminated by one `F`. Only an
             F-terminated line is complete.
  MEMORY     A pod can write faster than we ship, so a read is bounded to one batch.
             That bound is the per-pod memory ceiling; there is no separate window.

`stream()` is the whole public surface. It yields a batch and commits on the line
after, which runs only when control comes back — i.e. the consumer placed it. A failed
post or a cancellation is raised AT the yield, so the commit never runs and the batch
is re-read. At-least-once is therefore the shape of the code rather than a rule someone
has to keep; the validator dedupes the replay on a high watermark.
"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path
from typing import AsyncGenerator, Dict, List, Optional, Tuple

from loguru import logger

from .checkpoint import CheckpointStore
from .config import LogShipperConfig
from .models import ChutePod, LogLine

_VALID_STREAMS = {"stdout", "stderr"}
_VALID_TAGS = {"F", "P"}

# CRI log filename: "<restart>.log" (current) or "<restart>.log.<rotation-suffix>".
_LOG_NAME = re.compile(r"^(\d+)\.log(?:\.(.+))?$")


# ── CRI framing ─────────────────────────────────────────────────────────────


def parse_cri_line(raw: str) -> Optional[Tuple[str, str, str, str]]:
    """Parse one CRI line into (ts, stream, tag, message).

    Format: ``<RFC3339Nano> <stdout|stderr> <F|P> <message>``. None if malformed.
    """
    if not raw:
        return None
    parts = raw.split(" ", 3)
    if len(parts) < 3:
        return None
    ts, stream, tag = parts[0], parts[1], parts[2]
    message = parts[3] if len(parts) == 4 else ""
    if stream not in _VALID_STREAMS or tag not in _VALID_TAGS:
        return None
    return ts, stream, tag, message


def truncate_bytes(message: str, max_bytes: int) -> str:
    encoded = message.encode("utf-8", errors="replace")
    if len(encoded) <= max_bytes:
        return message
    return encoded[:max_bytes].decode("utf-8", errors="ignore")


def parse_logical_lines(
    data: bytes, max_line_bytes: int, *, flush_incomplete: bool = False
) -> List[Tuple[LogLine, int]]:
    """Complete logical lines in `data`, as (line, offset just past its `F`).

    A trailing physical line with no newline, and a trailing `P`-run whose `F` has
    not arrived, are both withheld — so the caller can commit through the last
    returned offset without ever shipping or skipping a fragment.

    `flush_incomplete` emits a withheld `P`-run anyway, truncated, ending at the last
    complete physical line. Only the caller knows when that is correct: it is a lie
    about the data, told because the alternative is worse (see `read`).
    """
    results: List[Tuple[LogLine, int]] = []
    partial: Dict[str, str] = {}
    last_ts: Dict[str, str] = {}
    i = 0
    while True:
        j = data.find(b"\n", i)
        if j == -1:
            break  # incomplete trailing physical line — leave it for next read
        end = j + 1
        parsed = parse_cri_line(data[i:j].decode("utf-8", errors="replace"))
        if parsed is not None:
            ts, stream, tag, message = parsed
            if tag == "P":
                partial[stream] = (partial.get(stream, "") + message)[:max_line_bytes]
                last_ts[stream] = ts
            else:
                full = partial.pop(stream, "") + message
                results.append(
                    (
                        LogLine(
                            ts=ts,
                            stream=stream,
                            log=truncate_bytes(full, max_line_bytes),
                        ),
                        end,
                    )
                )
        i = end
    if flush_incomplete and partial and not results:
        for stream, buf in partial.items():
            results.append(
                (
                    LogLine(
                        ts=last_ts[stream],
                        stream=stream,
                        log=truncate_bytes(buf, max_line_bytes),
                    ),
                    i,
                )
            )
    return results


# ── file discovery ──────────────────────────────────────────────────────────


def log_sort_key(path: Path) -> Tuple[int, int, str]:
    """Oldest to newest: lower restart index first, rotated before current."""
    match = _LOG_NAME.match(path.name)
    if not match:
        return (0, 0, path.name)
    index, suffix = int(match.group(1)), match.group(2)
    return (index, 0 if suffix else 1, suffix or "")


def log_files(pod_dir: Path, container_name: str) -> List[Path]:
    """Non-gz CRI log files for the chute container only, oldest to newest.

    Only the main container is captured, so the shipped stream stays
    single-container and therefore monotonic in ts — which is what the validator's
    high-watermark dedupe relies on.
    """
    container = pod_dir / container_name
    try:
        entries = [
            e
            for e in container.iterdir()
            if e.is_file() and ".log" in e.name and not e.name.endswith(".gz")
        ]
    except (FileNotFoundError, NotADirectoryError):
        return []
    entries.sort(key=log_sort_key)
    return entries


# ── the reader ──────────────────────────────────────────────────────────────


class PodLogReader:
    """Reads one pod's logs, surviving rotation and restart."""

    def __init__(
        self,
        pod: ChutePod,
        config: LogShipperConfig,
        checkpoints: CheckpointStore,
    ):
        self._pod = pod
        self._config = config
        self._checkpoints = checkpoints
        self._offsets: Dict[int, int] = {
            int(inode): offset
            for inode, offset in checkpoints.get(pod.config_id).items()
        }
        # Offsets for the batch handed out but not yet confirmed shipped.
        self._pending: List[Tuple[int, int]] = []

    async def stream(self) -> AsyncGenerator[List[LogLine], None]:
        """Yield batches, committing each once the consumer has placed it.

        The commit below the yield runs only when control returns here, which happens
        only if the consumer's body completed. If the post raised, or the task was
        cancelled, the exception is delivered AT the yield and the commit is skipped,
        so the batch is re-read next time round.

        Never await in a `finally` here: after GeneratorExit that is a RuntimeError.
        """
        while True:
            batch = self._read_next()
            if not batch:
                await asyncio.sleep(self._config.poll_interval_seconds)
                continue
            yield batch
            await self._commit()
            await asyncio.sleep(self._config.drain_interval_seconds)

    def _read_next(self) -> List[LogLine]:
        """One batch of complete lines, oldest file first. Empty when nothing is ready.

        Bounded by `batch_max_bytes` and `batch_max_lines`; the byte bound is also this
        pod's memory ceiling, since only one batch is ever held.
        """
        budget = self._config.batch_max_bytes
        room = self._config.batch_max_lines
        batch: List[LogLine] = []
        pending: List[Tuple[int, int]] = []
        files = log_files(self._pod_dir(), self._config.container_name)

        for path in files:
            if budget <= 0 or room <= 0:
                break
            try:
                stat = path.stat()
            except OSError:  # vanished mid-scan
                continue
            inode = stat.st_ino
            offset = self._offsets.get(inode, 0)
            if stat.st_size < offset:  # truncated — start over
                offset = 0
            available = stat.st_size - offset
            if available <= 0:
                continue
            to_read = min(available, budget)
            try:
                with path.open("rb") as handle:
                    handle.seek(offset)
                    data = handle.read(to_read)
            except OSError:  # vanished mid-read
                continue
            budget -= len(data)
            for line, rel_end in self._parse(data, complete_window=to_read < available):
                if room <= 0:
                    break
                batch.append(line)
                pending.append((inode, offset + rel_end))
                room -= 1

        self._purge_stale_offsets(files)
        self._pending = pending
        return batch

    async def _commit(self) -> None:
        """Persist progress through the batch just consumed."""
        for inode, end_offset in self._pending:
            if end_offset > self._offsets.get(inode, 0):
                self._offsets[inode] = end_offset
        self._pending = []
        await self._checkpoints.set(
            self._pod.config_id,
            {str(inode): offset for inode, offset in self._offsets.items()},
        )

    def _parse(self, data: bytes, *, complete_window: bool):
        """Parse a window, forcing progress when withholding would stall forever.

        A full read holding no `F` means the logical line is longer than a batch.
        Progress only advances on a returned line, so withholding here means the next
        read starts at the same offset and sees the same bytes — for good. That stalls
        this pod permanently, and because the loop never awaits on an empty read it
        also starves every other pod's shipper on the agent. So we truncate and move
        on, which loses part of one absurd line.
        """
        parsed = parse_logical_lines(data, self._config.max_line_bytes)
        if parsed or not complete_window:
            return parsed
        parsed = parse_logical_lines(
            data, self._config.max_line_bytes, flush_incomplete=True
        )
        if parsed:
            logger.warning(
                f"Log line exceeds the {self._config.batch_max_bytes} byte batch "
                f"for config_id={self._pod.config_id}; truncating"
            )
        return parsed

    def _pod_dir(self) -> Path:
        return self._config.pod_log_root / self._pod.log_dir_name

    def _purge_stale_offsets(self, files: List[Path]) -> None:
        """Drop offsets whose log file no longer exists.

        Without this, every rotation leaves a dead entry behind and the checkpoint —
        rewritten in full on each commit — grows for the life of the pod.

        Keyed on the full listing, not on the files this read happened to reach: a
        read that stops on budget has not reached the later files, and purging those
        would re-ship them from zero on the next pass.
        """
        live = set()
        for path in files:
            try:
                live.add(path.stat().st_ino)
            except OSError:
                continue
        for inode in [i for i in self._offsets if i not in live]:
            del self._offsets[inode]
