"""Persistent shipped-offset checkpoint: {config_id -> {inode -> byte offset}}.

The log file on disk is the durable buffer; this records how far we have
*successfully shipped* per file (keyed by inode, so it follows kubelet rotation
— a rename keeps the inode). On restart the streamer resumes from here; the
validator dedupes on ``(config_id, ts)`` for any bytes replayed at the edges.
Reconciled to the live pod set on every poll and evicted on pod-delete, so it
cannot grow unbounded.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Dict, Iterable

from loguru import logger

# Per-config offsets: inode (as str, for JSON) -> shipped byte offset.
Offsets = Dict[str, int]


class CheckpointStore:
    """Async-safe, atomically-persisted map of config_id -> per-inode offsets."""

    def __init__(self, path: Path):
        self._path = path
        self._data: Dict[str, Offsets] = {}
        self._lock = asyncio.Lock()

    async def load(self) -> None:
        """Load the checkpoint file if present; tolerate a missing/corrupt file."""
        async with self._lock:
            self._data = self._read()

    def _read(self) -> Dict[str, Offsets]:
        try:
            raw = self._path.read_text()
        except FileNotFoundError:
            return {}
        except OSError as exc:  # pragma: no cover - defensive
            logger.warning("Failed to read checkpoint file {}: {}", self._path, exc)
            return {}
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            logger.warning("Corrupt checkpoint file {}, starting empty", self._path)
            return {}
        if not isinstance(parsed, dict):
            return {}
        result: Dict[str, Offsets] = {}
        for config_id, offsets in parsed.items():
            if isinstance(offsets, dict):
                result[str(config_id)] = {
                    str(ino): int(off) for ino, off in offsets.items()
                }
        return result

    def get(self, config_id: str) -> Offsets:
        """Return a copy of the stored per-inode offsets ({} if none)."""
        return dict(self._data.get(config_id, {}))

    async def set(self, config_id: str, offsets: Offsets) -> None:
        """Replace the offsets for a config_id and flush to disk."""
        async with self._lock:
            self._data[config_id] = dict(offsets)
            self._flush()

    async def evict(self, config_id: str) -> None:
        async with self._lock:
            if self._data.pop(config_id, None) is not None:
                self._flush()

    async def reconcile(self, live_config_ids: Iterable[str]) -> int:
        """Drop entries for config_ids no longer present. Returns count removed."""
        live = set(live_config_ids)
        async with self._lock:
            stale = [cid for cid in self._data if cid not in live]
            for cid in stale:
                del self._data[cid]
            if stale:
                self._flush()
        return len(stale)

    def snapshot(self) -> Dict[str, Offsets]:
        return {cid: dict(offs) for cid, offs in self._data.items()}

    def _flush(self) -> None:
        """Atomic write: tmp file + rename, so a crash never leaves a partial file."""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(self._path.suffix + ".tmp")
        tmp.write_text(json.dumps(self._data, sort_keys=True))
        os.replace(tmp, self._path)
