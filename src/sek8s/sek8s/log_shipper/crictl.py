"""Pod discovery via the bundled `k3s crictl`, through a restricted wrapper.

No k8s API access: the CRI sandbox metadata carries the pod's k8s labels and
UID, which is everything the agent needs. `k3s ctr` does not expose k8s pod
labels, so crictl is the only source.
"""

from __future__ import annotations

import asyncio
import json
import re
from typing import List

from loguru import logger

from .config import LogShipperConfig

# CrictlError is defined in exceptions.py and re-exported here: callers (agent,
# tests) do `from ...crictl import CrictlError`.
from .exceptions import CrictlError
from .models import ChutePod

# A k8s label value, per apiserver `IsValidLabelValue`: alphanumeric ends, dashes,
# underscores and dots between, 63 bytes max.
#
# Re-asserted here rather than inherited, because `config_id` is concatenated into an
# egress URL path (`config.logs_url`) that the shipper requests with the VM's mTLS
# client identity. A `/` in that value is NOT escaped -- it is structural by the time
# yarl parses the assembled string, and `..` is then resolved away, so the request
# leaves `/instances/launch_config/` entirely. The apiserver refuses `/` in a label,
# which is the only reason that is unreachable; nothing in this module said so, and
# nothing here would notice if pod metadata ever arrived from somewhere else.
#
# Valid values already satisfy this by construction, so it rejects nothing real.
_LABEL_VALUE = re.compile(r"^[A-Za-z0-9]([-A-Za-z0-9_.]*[A-Za-z0-9])?$")
_LABEL_VALUE_MAX = 63


def _is_label_value(value: str) -> bool:
    return len(value) <= _LABEL_VALUE_MAX and bool(_LABEL_VALUE.match(value))


async def run_crictl(config: LogShipperConfig, args: List[str]) -> str:
    """Invoke the restricted crictl wrapper and return stdout.

    Uses create_subprocess_exec (no shell) against the allowlisted wrapper path.
    """
    command = [str(config.crictl_wrapper_path), *args]
    logger.debug("Running crictl wrapper: {}", command)
    try:
        process = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError as exc:
        raise CrictlError(
            f"crictl wrapper not found: {config.crictl_wrapper_path}"
        ) from exc

    try:
        stdout_bytes, stderr_bytes = await asyncio.wait_for(
            process.communicate(), timeout=config.command_timeout_seconds
        )
    except asyncio.TimeoutError as exc:
        try:
            process.kill()
        except ProcessLookupError:  # pragma: no cover - race on already-exited proc
            pass
        raise CrictlError("crictl wrapper timed out") from exc

    if process.returncode != 0:
        stderr = stderr_bytes.decode("utf-8", errors="replace").strip()
        raise CrictlError(f"crictl wrapper exited {process.returncode}: {stderr}")
    return stdout_bytes.decode("utf-8", errors="replace")


def parse_chute_pods(raw: str, config: LogShipperConfig) -> List[ChutePod]:
    """Filter `crictl pods -o json` output to the chute pods we should capture.

    A pod qualifies when it is in the configured namespace, carries the label
    selector (key=value), and has the config-id label.
    """
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise CrictlError("crictl returned non-JSON output") from exc

    items = payload.get("items") or []
    pods: List[ChutePod] = []
    for item in items:
        metadata = item.get("metadata") or {}
        labels = item.get("labels") or {}
        name = metadata.get("name")
        uid = metadata.get("uid")
        namespace = metadata.get("namespace")
        config_id = labels.get(config.config_id_label)

        if namespace != config.namespace:
            continue
        if labels.get(config.selector_key) != config.selector_value:
            continue
        if not (name and uid and config_id):
            continue
        if not _is_label_value(config_id):
            logger.warning(
                f"Skipping chute pod {name}: config-id label is not a valid label "
                f"value (length {len(config_id)})"
            )
            continue

        deployment_id = labels.get(config.deployment_id_label, "")
        if deployment_id and not _is_label_value(deployment_id):
            logger.warning(
                f"Skipping chute pod {name}: deployment-id label is not a valid "
                f"label value (length {len(deployment_id)})"
            )
            continue

        pods.append(
            ChutePod(
                config_id=config_id,
                name=name,
                uid=uid,
                namespace=namespace,
                deployment_id=deployment_id,
                state=item.get("state", ""),
                labels=labels,
            )
        )
    return pods


async def list_chute_pods(config: LogShipperConfig) -> List[ChutePod]:
    """Discover the current set of chute pods on this node."""
    raw = await run_crictl(config, ["pods", "-o", "json"])
    return parse_chute_pods(raw, config)
