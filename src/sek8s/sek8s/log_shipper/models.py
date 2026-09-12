"""Internal data models for the chute log shipper.

The wire body is deliberately minimal — only the log lines. Everything else
(config_id, miner_hotkey, vm_name, server) is derived validator-side from the
request path + mTLS leaf + proxy, so the guest never self-asserts identity.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict

from pydantic import BaseModel


class LogLine(BaseModel):
    """One CRI log line as shipped to the validator."""

    ts: str  # RFC3339 nanosecond timestamp, e.g. 2026-07-27T00:00:00.123456789Z
    stream: str  # "stdout" | "stderr"
    log: str


class LogBatch(BaseModel):
    """Request body for POST /instances/launch_config/{config_id}/logs.

    `deployment_id` is the sole top-level metadata field — non-security
    correlation data (Grafana filtering) the validator cannot derive from
    `config_id` in the pre-registration window. All identity is derived
    server-side from the path + mTLS leaf + proxy.
    """

    deployment_id: str
    logs: list[LogLine]


@dataclass(frozen=True)
class ChutePod:
    """A chute pod discovered from `crictl pods -o json`.

    Only the fields the agent actually needs: config_id (→ path), deployment_id
    (→ shipped top-level), the on-disk log-dir components (namespace/name/uid),
    and the sandbox state (terminal detection). The chute-id label is
    intentionally not carried — the validator derives that identity server-side.
    """

    config_id: str
    name: str
    uid: str
    namespace: str
    deployment_id: str = ""
    state: str = ""
    labels: Dict[str, str] = field(default_factory=dict)

    @property
    def log_dir_name(self) -> str:
        """CRI pod log directory: <namespace>_<name>_<uid>."""
        return f"{self.namespace}_{self.name}_{self.uid}"
