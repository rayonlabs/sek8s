"""Configuration for the chute log shipper agent.

Env-driven via pydantic-settings, following the SystemManager* config style
(aliased env fields, no populate_by_name). No hardcoded URLs, paths, or
credentials — everything below is overridable from the rendered env file.
"""

import json
from pathlib import Path
from typing import Any, List

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sek8s_common.constants import MTLS_CLIENT_CERT, MTLS_CLIENT_KEY


class LogShipperConfig(BaseSettings):
    """Settings for the chute-log-shipper systemd service."""

    # ── Validator egress (CVM mTLS host) ────────────────────────────────────
    validator_base_url: str = Field(
        default="https://cvm.chutes.ai",
        alias="VALIDATOR_BASE_URL",
        description="Base URL of the dedicated CVM mTLS proxy that fronts the ingest endpoint",
    )
    # The VM's shared mTLS client identity (same env name + default as the cosign client): the one
    # cert presented for all CVM->Chutes mTLS, which the API validates against the /provision CA.
    mtls_cert_path: Path = Field(
        default=Path(MTLS_CLIENT_CERT),
        alias="SEK8S_MTLS_CLIENT_CERT",
        description="Per-boot VM mTLS client leaf presented as the log-egress identity",
    )
    mtls_key_path: Path = Field(
        default=Path(MTLS_CLIENT_KEY),
        alias="SEK8S_MTLS_CLIENT_KEY",
        description="Private key for the mTLS client leaf",
    )
    # HTTP status the validator returns to signal "stop capturing this pod".
    # Any other 2xx = keep sending; non-2xx / connection error = transient retry.
    stop_status_code: int = Field(default=204, alias="STOP_STATUS_CODE", ge=200, le=599)
    # Statuses that mean "this shipment can never succeed" (unknown config_id /
    # cert-ownership rejection): stop and log rather than retry forever.
    terminal_status_codes: List[int] = Field(
        default_factory=lambda: [403, 404],
        alias="TERMINAL_STATUS_CODES",
        description="Non-2xx statuses treated as terminal rejects (stop + log, no retry)",
    )

    # ── Pod discovery (CRI via restricted crictl wrapper) ───────────────────
    namespace: str = Field(default="chutes", alias="POD_NAMESPACE")
    label_selector: str = Field(
        default="chutes/chute=true",
        alias="LABEL_SELECTOR",
        description="Single key=value label a pod sandbox must carry to be captured",
    )
    config_id_label: str = Field(
        default="chutes/config-id",
        alias="CONFIG_ID_LABEL",
        description="Pod label holding the launch config id (→ request path)",
    )
    deployment_id_label: str = Field(
        default="chutes/deployment-id",
        alias="DEPLOYMENT_ID_LABEL",
        description="Pod label holding the deployment id (→ shipped top-level; validator "
        "cannot derive it from config_id in the pre-registration window)",
    )
    crictl_wrapper_path: Path = Field(
        default=Path("/usr/local/bin/crictl-pods-helper"),
        alias="CRICTL_WRAPPER_PATH",
        description="Restricted wrapper allowing only read verbs (pods -o json / ps)",
    )
    pod_log_root: Path = Field(
        default=Path("/var/log/pods"),
        alias="POD_LOG_ROOT",
        description="CRI on-disk pod log root: <root>/<ns>_<name>_<uid>/<container>/*.log",
    )
    container_name: str = Field(
        default="chute",
        alias="CONTAINER_NAME",
        description="Only this pod container's logs are shipped. Admission denies a chute "
        "workload with no container of this name, so the assumption holds; init/sidecar "
        "containers are skipped to keep the stream single-container, hence monotonic in ts, "
        "which the validator's high-watermark dedupe relies on.",
    )
    command_timeout_seconds: float = Field(
        default=15.0, alias="COMMAND_TIMEOUT_SECONDS", gt=0.0, le=120.0
    )

    # ── Offset checkpoint persistence ───────────────────────────────────────
    checkpoint_path: Path = Field(
        default=Path("/var/lib/chute-log-shipper/checkpoint.json"),
        alias="CHECKPOINT_PATH",
        description="Persisted {config_id -> {inode -> shipped byte offset}}, "
        "reconciled to the live pod set",
    )

    # ── Streaming window / cadence ──────────────────────────────────────────
    poll_interval_seconds: float = Field(
        default=10.0,
        alias="POLL_INTERVAL_SECONDS",
        gt=0.0,
        le=3600.0,
        description="Idle sleep when caught up to EOF (only new bytes are read next cycle)",
    )
    drain_interval_seconds: float = Field(
        default=0.1,
        alias="DRAIN_INTERVAL_SECONDS",
        gt=0.0,
        le=60.0,
        description="Gap between batches while draining a backlog. Bounds the request rate "
        "against the validator, and guarantees the loop always yields — a chute writing "
        "faster than we ship would otherwise never let it await.",
    )
    batch_max_lines: int = Field(default=500, alias="BATCH_MAX_LINES", ge=1, le=100_000)
    batch_max_bytes: int = Field(
        default=1_048_576,
        alias="BATCH_MAX_BYTES",
        ge=1024,
        le=64 * 1_048_576,
        description="Bytes read per batch, which is also the deterministic memory ceiling: "
        "the reader hands out one batch at a time, so the two are the same number. Must "
        "exceed the CRI physical line cap (~16 KiB) so a read always makes progress.",
    )
    max_line_bytes: int = Field(
        default=16_384,
        alias="MAX_LINE_BYTES",
        ge=256,
        le=1_048_576,
        description="A single reassembled logical line is truncated to this many bytes",
    )

    # ── Concurrency ─────────────────────────────────────────────────────────
    max_concurrent_pods: int = Field(
        default=32, alias="MAX_CONCURRENT_PODS", ge=1, le=1024
    )

    # ── POST retry/backoff ──────────────────────────────────────────────────
    request_timeout_seconds: float = Field(
        default=30.0, alias="REQUEST_TIMEOUT_SECONDS", gt=0.0, le=600.0
    )
    retry_max_attempts: int = Field(default=5, alias="RETRY_MAX_ATTEMPTS", ge=1, le=100)
    retry_base_delay_seconds: float = Field(
        default=1.0, alias="RETRY_BASE_DELAY_SECONDS", gt=0.0, le=60.0
    )
    retry_max_delay_seconds: float = Field(
        default=30.0, alias="RETRY_MAX_DELAY_SECONDS", gt=0.0, le=600.0
    )

    debug: bool = Field(default=False, alias="DEBUG")

    model_config = SettingsConfigDict(
        env_file_encoding="utf-8", case_sensitive=False, extra="ignore"
    )

    @field_validator("terminal_status_codes", mode="before")
    @classmethod
    def _parse_terminal_codes(cls, v: Any) -> List[int]:
        """Accept a JSON array or comma-separated list of status codes."""
        if isinstance(v, (list, tuple)):
            return [int(x) for x in v]
        if isinstance(v, str):
            try:
                parsed = json.loads(v)
                if isinstance(parsed, list):
                    return [int(x) for x in parsed]
            except json.JSONDecodeError:
                pass
            return [int(x.strip()) for x in v.split(",") if x.strip()]
        return v  # pragma: no cover - defensive; let pydantic validate other types

    @field_validator("label_selector")
    @classmethod
    def _validate_selector(cls, v: str) -> str:
        if "=" not in v:
            raise ValueError("label_selector must be of the form key=value")
        key, _, value = v.partition("=")
        if not key.strip() or not value.strip():
            raise ValueError("label_selector must be of the form key=value")
        return v

    @property
    def selector_key(self) -> str:
        return self.label_selector.partition("=")[0].strip()

    @property
    def selector_value(self) -> str:
        return self.label_selector.partition("=")[2].strip()

    def logs_url(self, config_id: str) -> str:
        """Ingest endpoint for a launch config (config_id lives in the path)."""
        base = self.validator_base_url.rstrip("/")
        return f"{base}/instances/launch_config/{config_id}/logs"
