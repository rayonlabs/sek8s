"""Watch for chute pods and keep one shipper alive per pod.

    while True:
        pods = list_chute_pods()
        start a shipper for each new pod, stop the ones whose pod is gone
        sleep

The agent deals in pods and shippers and nothing below that: a shipper owns its own
task, and the reader inside it owns files and offsets. Fail-closed by construction —
a crictl error or a dead validator never breaks the loop, it logs and retries on the
next poll.
"""

from __future__ import annotations

import asyncio
import ssl
from typing import Dict, List, Optional, cast

import aiohttp
from loguru import logger

from .checkpoint import CheckpointStore
from .config import LogShipperConfig
from .crictl import CrictlError, list_chute_pods
from .models import ChutePod
from .shipper import PodLogShipper


def build_ssl_context(config: LogShipperConfig) -> ssl.SSLContext:
    """TLS context presenting the registry mTLS leaf as the client identity.

    Server verification stays on (default CA bundle) — the CVM proxy presents a
    publicly-trusted cert.
    """
    context = ssl.create_default_context()
    context.load_cert_chain(
        certfile=str(config.mtls_cert_path), keyfile=str(config.mtls_key_path)
    )
    return context


class LogShipperAgent:
    """Discovers chute pods and keeps their capture running."""

    def __init__(self, config: LogShipperConfig):
        self._config = config
        self._checkpoints = CheckpointStore(config.checkpoint_path)
        # config_id -> shipper. Holds finished shippers too; see _sync.
        self._shippers: Dict[str, PodLogShipper] = {}
        self._session: Optional[aiohttp.ClientSession] = None

    async def run(self) -> None:
        await self._checkpoints.load()
        connector = aiohttp.TCPConnector(ssl=build_ssl_context(self._config))
        async with aiohttp.ClientSession(connector=connector) as session:
            self._session = session
            try:
                while True:
                    await self._poll()
                    await asyncio.sleep(self._config.poll_interval_seconds)
            finally:
                await self._stop_all()

    async def _poll(self) -> None:
        """One discovery pass. A failed pass changes nothing and retries later."""
        try:
            pods = await list_chute_pods(self._config)
        except CrictlError as exc:
            logger.warning(f"Pod discovery failed (will retry): {exc}")
            return
        await self._sync(pods)
        await self._checkpoints.reconcile(pod.config_id for pod in pods)

    async def _sync(self, pods: List[ChutePod]) -> None:
        """Start a shipper for each new pod; stop the ones whose pod has gone.

        A finished shipper stays in the map. Its `running` is False, but the key is
        still there, and that is what stops us restarting capture the validator
        deliberately ended for a pod that is still present. The key goes only when the
        pod does — so one map answers both "am I capturing this?" and "have I already
        handled this?", with no second set to keep in step.
        """
        live = {pod.config_id: pod for pod in pods}

        for config_id in [c for c in self._shippers if c not in live]:
            await self._shippers.pop(config_id).stop()

        pending = [
            pod for config_id, pod in live.items() if config_id not in self._shippers
        ]
        if not pending:
            return

        # Finished shippers hold no resources, so only running ones count against the cap.
        capacity = self._config.max_concurrent_pods - sum(
            1 for shipper in self._shippers.values() if shipper.running
        )
        if capacity < len(pending):
            logger.warning(
                f"At capture capacity ({self._config.max_concurrent_pods} pods); "
                f"starting {max(capacity, 0)} of {len(pending)} new pod(s), "
                f"deferring the rest to a later poll"
            )
        for pod in pending[: max(capacity, 0)]:
            shipper = PodLogShipper(
                pod,
                self._config,
                cast(aiohttp.ClientSession, self._session),
                self._checkpoints,
            )
            shipper.start()
            self._shippers[pod.config_id] = shipper

    async def _stop_all(self) -> None:
        shippers = list(self._shippers.values())
        self._shippers.clear()
        for shipper in shippers:
            await shipper.stop()
