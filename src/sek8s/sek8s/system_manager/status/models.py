"""Status submodule: dataclasses and service definitions."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict


@dataclass(frozen=True)
class ServiceDefinition:
    service_id: str
    unit: str
    description: str
    masked_ok: bool = False


@dataclass
class CommandResult:
    exit_code: int
    stdout: str
    stderr: str
    stdout_truncated: bool
    stderr_truncated: bool


SERVICE_ALLOWLIST: Dict[str, ServiceDefinition] = {
    "admission-controller": ServiceDefinition(
        service_id="admission-controller",
        unit="admission-controller.service",
        description="sek8s admission controller",
    ),
    "opa": ServiceDefinition(
        service_id="opa",
        unit="opa.service",
        description="Open Policy Agent backing the admission controller",
    ),
    "system-manager": ServiceDefinition(
        service_id="system-manager",
        unit="system-manager.service",
        description="sek8s system manager (status + cache)",
    ),
    "attestation-service": ServiceDefinition(
        service_id="attestation-service",
        unit="attestation-service.service",
        description="TDX/nvtrust attestation service",
    ),
    "chute-log-shipper": ServiceDefinition(
        service_id="chute-log-shipper",
        unit="chute-log-shipper.service",
        description="Chute crash-log capture agent (ships pre-registration pod logs to the validator)",
    ),
    "k3s": ServiceDefinition(
        service_id="k3s",
        unit="k3s.service",
        description="Lightweight Kubernetes control plane",
    ),
    "storage-bind-mounts": ServiceDefinition(
        service_id="storage-bind-mounts",
        unit="setup-storage-bind-mounts.service",
        description="Sync and bind-mount storage volume directories (k3s, kubelet, rancher config, etc.)",
    ),
    "nvidia-persistenced": ServiceDefinition(
        service_id="nvidia-persistenced",
        unit="nvidia-persistenced.service",
        description="NVIDIA persistence daemon",
    ),
    "nvidia-fabricmanager": ServiceDefinition(
        service_id="nvidia-fabricmanager",
        unit="nvidia-fabricmanager.service",
        description="NVIDIA fabric manager",
        masked_ok=True,
    ),
    "infiniband-config": ServiceDefinition(
        service_id="infiniband-config",
        unit="infiniband-config.service",
        description="Configure InfiniBand: mask services and disable ib_umad when no Mellanox devices present",
    ),
}
