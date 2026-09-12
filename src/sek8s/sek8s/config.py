"""
Configuration management for admission controller using Pydantic v2.
"""

import json
import logging
import os
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional
from urllib.parse import urlparse

from pydantic import BaseModel, Field, PrivateAttr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sek8s_common.config import AuthConfig, ServerConfig
from substrateinterface import Keypair

logger = logging.getLogger(__name__)


class NamespacePolicy(BaseModel):
    """Policy configuration for a namespace."""

    mode: Literal["enforce", "warn", "monitor"] = "enforce"
    exempt: bool = False


class AttestationServiceConfig(ServerConfig):

    hostname: Optional[str] = os.getenv("HOSTNAME")

    model_config = SettingsConfigDict(
        env_file_encoding="utf-8", case_sensitive=False, extra="ignore"
    )


class SystemStatusConfig(AuthConfig):
    """Configuration for the read-only system status service.

    Inherits auth capabilities from AuthConfig for endpoints like shutdown.
    """

    require_tls: bool = Field(default=False, alias="REQUIRE_TLS")

    max_output_bytes: int = Field(
        default=16_384, alias="MAX_OUTPUT_BYTES", ge=1024, le=1_000_000
    )
    command_timeout_seconds: float = Field(
        default=10.0,
        alias="COMMAND_TIMEOUT_SECONDS",
        gt=0.0,
        le=60.0,
    )
    log_tail_default: int = Field(default=200, alias="LOG_TAIL_DEFAULT", ge=1, le=5000)
    log_tail_max: int = Field(default=1000, alias="LOG_TAIL_MAX", ge=100, le=10_000)
    log_window_max_minutes: int = Field(
        default=1440,
        alias="LOG_WINDOW_MAX_MINUTES",
        ge=1,
        le=7 * 24 * 60,
    )

    model_config = SettingsConfigDict(
        env_file_encoding="utf-8", case_sensitive=False, extra="ignore"
    )


class SystemManagerConfig(ServerConfig):
    """Configuration for the system manager process only (binding, TLS, debug).

    Used by the system-manager entrypoint to run the server. Status and cache
    routes use their own config classes (SystemStatusConfig, CacheConfig).
    """

    require_tls: bool = Field(default=False, alias="REQUIRE_TLS")

    model_config = SettingsConfigDict(
        env_file_encoding="utf-8", case_sensitive=False, extra="ignore"
    )


class CacheConfig(AuthConfig):
    """Configuration for the cache router (HF cache download, delete, cleanup, overview)."""

    cache_base: str = Field(
        default="/var/snap/cache",
        alias="HF_CACHE_BASE",
        description="Base directory for HF cache (per-chute dirs under this)",
    )
    validator_base_url: str = Field(
        default="https://api.chutes.ai",
        alias="VALIDATOR_BASE_URL",
        description="Base URL for validator API (e.g. GET /chutes/{chute_id}/hf_info, GET /misc/hf_repo_info)",
    )

    model_config = SettingsConfigDict(
        env_file_encoding="utf-8", case_sensitive=False, extra="ignore"
    )


cache_config = CacheConfig()


class ImageConfig(AuthConfig):
    """Configuration for the images router (k3s/containerd image management)."""

    image_pull_allowed_registries: List[str] = Field(
        default_factory=lambda: ["registry.chutes.ai"],
        alias="IMAGE_PULL_ALLOWED_REGISTRIES",
        description="JSON array or comma-separated list of allowed registries for image pull",
    )
    image_pull_timeout_seconds: float = Field(
        default=1200.0,
        alias="IMAGE_PULL_TIMEOUT_SECONDS",
        gt=0,
        le=3600,
        description="Timeout for image pull in seconds",
    )
    image_pull_default_org: str = Field(
        default="chutes",
        alias="IMAGE_PULL_DEFAULT_ORG",
        description="Default org when using short form (repo:tag)",
    )

    model_config = SettingsConfigDict(
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    @field_validator("image_pull_allowed_registries", mode="before")
    @classmethod
    def parse_allowed_registries(cls, v: Any) -> List[str]:
        """Parse from JSON array or comma-separated string."""
        if isinstance(v, list):
            return [str(x).strip() for x in v if x]
        if isinstance(v, str):
            try:
                parsed = json.loads(v)
                if isinstance(parsed, list):
                    return [str(x).strip() for x in parsed if x]
            except json.JSONDecodeError:
                pass
            return [x.strip() for x in v.split(",") if x.strip()]
        return []


image_config = ImageConfig()


class AdmissionConfig(ServerConfig):
    """Main configuration for admission controller using Pydantic v2."""

    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", case_sensitive=False, extra="ignore"
    )

    # OPA configuration
    opa_url: str = Field(default="http://localhost:8181", alias="OPA_URL")
    opa_timeout: float = Field(default=5.0, alias="OPA_TIMEOUT", gt=0)

    # Policy configuration
    policy_path: Path = Field(default=Path("/etc/opa/policies"), alias="POLICY_PATH")

    # Registry allowlist - expects JSON array from environment
    allowed_registries: List[str] = Field(
        default=["docker.io", "gcr.io", "quay.io", "registry.chutes.ai"],
        alias="ALLOWED_REGISTRIES",
        description="JSON array of allowed registries",
    )

    # Cache configuration
    cache_enabled: bool = Field(default=True, alias="CACHE_ENABLED")
    cache_ttl: int = Field(default=300, alias="CACHE_TTL", ge=0)

    # Enforcement configuration
    enforcement_mode: Literal["enforce", "warn", "monitor"] = Field(
        default="enforce", alias="ENFORCEMENT_MODE"
    )

    # Namespace policies - expects JSON object from environment.
    # All namespaces default to enforce; OPA rules handle user-based
    # exemptions for system/controller users.
    namespace_policies: Dict[str, NamespacePolicy] = Field(
        default={
            "default": NamespacePolicy(mode="enforce", exempt=False),
        },
        alias="NAMESPACE_POLICIES",
        description="JSON object of namespace policies",
    )

    # Metrics configuration
    metrics_enabled: bool = Field(default=True, alias="METRICS_ENABLED")

    # Authorization webhook: pod name prefixes the miner is allowed to read logs
    # from in the chutes namespace. All other pod logs are denied for the miner.
    authz_allowed_log_prefixes: List[str] = Field(
        default=["agent-", "chutes-registry-", "failed-chute-cleanup-"],
        alias="AUTHZ_ALLOWED_LOG_PREFIXES",
        description="Pod name prefixes the miner may read logs from in chutes namespace",
    )

    # Cosign public keys for chutes namespace enforcement.
    # Keys are fetched at boot by the fetch-signing-keys initramfs script,
    # RSA-verified against the attested root key, and written to tmpfs.
    chutes_public_key_path: Path = Field(
        default=Path("/run/chutes/signing-keys/cosign/chutes.pub"),
        alias="CHUTES_PUBLIC_KEY_PATH",
        description="Path to cosign public key for registry.chutes.ai image signing enforcement",
    )
    dockerhub_public_key_path: Path = Field(
        default=Path("/run/chutes/signing-keys/cosign/dockerhub.pub"),
        alias="DOCKERHUB_PUBLIC_KEY_PATH",
        description="Path to cosign public key for Docker Hub image signing enforcement",
    )

    @field_validator("namespace_policies", mode="before")
    @classmethod
    def parse_namespace_policies(cls, v: Any) -> Dict[str, NamespacePolicy]:
        """Parse namespace policies from dict, ensuring NamespacePolicy objects."""
        if isinstance(v, dict):
            return {
                ns: NamespacePolicy(**policy) if isinstance(policy, dict) else policy
                for ns, policy in v.items()
            }
        return v

    @field_validator("tls_cert_path", "tls_key_path", mode="after")
    @classmethod
    def validate_tls_paths(cls, v: Optional[Path]) -> Optional[Path]:
        """Validate that TLS paths exist if specified."""
        if v is not None and not v.exists():
            raise ValueError(f"TLS path does not exist: {v}")
        return v

    @field_validator("policy_path", mode="after")
    @classmethod
    def validate_policy_path(cls, v: Path) -> Path:
        """Ensure policy path exists or can be created."""
        if not v.exists():
            v.mkdir(parents=True, exist_ok=True)
        return v

    def get_namespace_policy(self, namespace: str) -> NamespacePolicy:
        """Get policy for a specific namespace."""
        if namespace in self.namespace_policies:
            return self.namespace_policies[namespace]
        return self.namespace_policies.get("default", NamespacePolicy())

    def is_namespace_exempt(self, namespace: str) -> bool:
        """Check if namespace is exempt from admission control."""
        policy = self.get_namespace_policy(namespace)
        return policy.exempt

    def export_json(self) -> str:
        """Export configuration as JSON."""
        return self.model_dump_json(indent=2, exclude_unset=False)

    def export_dict(self) -> dict:
        """Export configuration as dictionary."""
        return self.model_dump(exclude_unset=False)


class OPAConfig(BaseSettings):
    """Configuration specific to OPA."""

    opa_binary_path: Path = Field(
        default=Path("/usr/local/bin/opa"), alias="OPA_BINARY_PATH"
    )
    opa_log_level: Literal["debug", "info", "warn", "error"] = Field(
        default="info", alias="OPA_LOG_LEVEL"
    )
    opa_decision_logs: bool = Field(default=False, alias="OPA_DECISION_LOGS")

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
    )


class CosignVerificationConfig(BaseSettings):
    """Base verification configuration that can be inherited at any level."""

    require_signature: bool = True
    verification_method: Literal["key", "keyless", "disabled"] = "key"

    # Key-based verification
    public_key: Optional[Path] = None

    # Keyless verification
    keyless_identity_regex: Optional[str] = None
    keyless_issuer: Optional[str] = None

    # Connection options
    allow_http: bool = False
    allow_insecure: bool = False

    # Transparency log
    rekor_url: str = "https://rekor.sigstore.dev"
    fulcio_url: str = "https://fulcio.sigstore.dev"

    model_config = SettingsConfigDict(case_sensitive=False)


class CosignRepositoryConfig(CosignVerificationConfig):
    """Repository-specific cosign configuration."""

    repository: str  # e.g., "chutes-agent" or "myapp"


class CosignOrganizationConfig(CosignVerificationConfig):
    """Organization-specific cosign configuration."""

    organization: str  # e.g., "parachutes", "bitnami", "library"

    # Optional repository-level overrides
    repositories: Dict[str, CosignRepositoryConfig] = Field(default_factory=dict)


class CosignRegistryConfig(CosignVerificationConfig):
    """Registry-level cosign configuration with nested org/repo structure."""

    registry: str  # e.g., "docker.io", "gcr.io", "registry.k8s.io"

    # Optional organization-level configs
    organizations: Dict[str, CosignOrganizationConfig] = Field(default_factory=dict)


class DigestPinEntry(BaseModel):
    """An image eligible for digest pinning by the mutating admission webhook.

    Only whitelisted images get the TTL + digest-pin optimization. All other
    images pass through the mutating webhook unmodified.
    """

    image: str = Field(
        ...,
        description="Fully-qualified image name without tag (e.g. 'docker.io/parachutes/failed-chute-cleanup')."
        + " Exact match only.",
    )
    ttl: int = Field(
        default=3600,
        ge=60,
        description="Seconds to pin the verified digest before requiring a fresh cosign verification.",
    )


class CosignConfig(BaseSettings):
    """Configuration for Cosign integration (Phase 4b).

    - ``success_cache_ttl_seconds`` / ``failure_cache_ttl_seconds`` — digest-pinned
      (``@sha256:…``) success/failure TTLs. Tag-only refs never cache success (tag may move).
    - ``tag_failure_cache_ttl_seconds`` — for tag-only refs, how long to remember
      invalid/missing signature so admission retries do not hammer the registry. ``0`` disables.
    - ``digest_pin_whitelist`` — images whose tags can be pinned to a verified digest
      by the mutating webhook (loaded from ``pin_digest_whitelist`` in cosign-registries.json).

    Environment (one alias each): ``COSIGN_SUCCESS_CACHE_TTL``,
    ``COSIGN_FAILURE_CACHE_TTL``, ``COSIGN_TAG_FAILURE_CACHE_TTL`` (TTL values are seconds).
    """

    success_cache_ttl_seconds: int = Field(
        default=3600,
        ge=0,
        description="Seconds to cache a successful verify for a digest-pinned image.",
        validation_alias="COSIGN_SUCCESS_CACHE_TTL",
    )
    failure_cache_ttl_seconds: int = Field(
        default=600,
        ge=0,
        description="Seconds to cache a failed verify for a digest-pinned image.",
        validation_alias="COSIGN_FAILURE_CACHE_TTL",
    )
    tag_failure_cache_ttl_seconds: int = Field(
        default=300,
        ge=0,
        description="Seconds to cache invalid/missing signature for tag-only refs (0 = no cache).",
        validation_alias="COSIGN_TAG_FAILURE_CACHE_TTL",
    )
    tag_pin_default_ttl_seconds: int = Field(
        default=3600,
        ge=60,
        description="Default TTL for digest pinning when a whitelist entry omits ttl.",
        validation_alias="COSIGN_TAG_PIN_DEFAULT_TTL",
    )
    digest_pin_whitelist: List[DigestPinEntry] = Field(
        default_factory=list,
        description="Images eligible for mutating webhook digest pinning (whitelist-only).",
    )

    # Cosign config
    oidc_identity_regex: str = Field(default="^https://github.com/your-org/.*")
    oidc_issuer: str = Field(default="https://token.actions.githubusercontent.com")
    cosign_rekor_url: str = Field(default="https://rekor.sigstore.dev")
    fulcio_url: str = Field(default="https://fulcio.sigstore.dev")

    # Registry configurations - loaded from file or defaults
    registry_configs: List[CosignRegistryConfig] = Field(
        default_factory=list, description="List of cosign configurations per registry"
    )

    # Cosign config file path
    cosign_registries_file: Optional[Path] = Field(
        default=None, description="Path to cosign registry configuration JSON file"
    )

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        populate_by_name=True,
    )

    @field_validator("registry_configs", mode="before")
    @classmethod
    def parse_registry_configs(cls, v: Any) -> List[CosignRegistryConfig]:
        """Parse cosign registry configs from list of dicts."""
        if isinstance(v, list):
            configs = []
            for item in v:
                if isinstance(item, dict):
                    configs.append(CosignRegistryConfig(**item))
                elif isinstance(item, CosignRegistryConfig):
                    configs.append(item)
            return configs
        return v

    def __init__(self, **kwargs):
        """Initialize cosign config with support for config file."""
        super().__init__(**kwargs)

        # Load registry configs after initialization
        self._load_registry_configs()

    def _load_registry_configs(self) -> None:
        """Load registry configurations from file or use defaults."""
        # If configs are already populated, don't override
        if self.registry_configs:
            logger.info(
                f"Using {len(self.registry_configs)} pre-configured registry configs"
            )
            return

        # Try to load from config file
        config_file_path = self.cosign_registries_file
        if not config_file_path:
            config_file_path = Path("/etc/admission-controller/cosign-registries.json")

        if config_file_path and config_file_path.exists():
            try:
                with open(config_file_path, "r") as f:
                    config_data = json.load(f)

                # Handle different JSON structures
                if isinstance(config_data, dict) and "registries" in config_data:
                    registry_data = config_data["registries"]
                elif isinstance(config_data, list):
                    registry_data = config_data
                else:
                    logger.warning(
                        f"Invalid cosign config format in {config_file_path}"
                    )
                    registry_data = []

                # Parse registry configurations
                configs = []
                for item in registry_data:
                    if isinstance(item, dict):
                        # Convert string paths to Path objects
                        if "public_key" in item and item["public_key"]:
                            item["public_key"] = Path(item["public_key"])
                        configs.append(CosignRegistryConfig(**item))

                self.registry_configs = configs
                logger.info(
                    f"Loaded {len(configs)} registry configs from {config_file_path}"
                )

                # Load digest pin whitelist from the same config file
                if (
                    isinstance(config_data, dict)
                    and "pin_digest_whitelist" in config_data
                ):
                    pin_list = config_data["pin_digest_whitelist"]
                    if isinstance(pin_list, list):
                        entries = []
                        for item in pin_list:
                            if isinstance(item, dict):
                                if "ttl" not in item:
                                    item["ttl"] = self.tag_pin_default_ttl_seconds
                                entries.append(DigestPinEntry(**item))
                        self.digest_pin_whitelist = entries
                        logger.info(
                            "Loaded %d digest pin whitelist entries from %s",
                            len(entries),
                            config_file_path,
                        )

            except Exception as e:
                logger.error(
                    f"Failed to load cosign config from {config_file_path}: {e}"
                )
        else:
            # Fall back to default configuration
            logger.info("Using default cosign registry configuration")
            self.registry_configs = [
                CosignRegistryConfig(
                    registry="*",
                    require_signature=True,
                    verification_method="key",
                    # Match the boot-fetched key path (required_key_paths / the rendered
                    # cosign-registries.json), not the retired /etc/admission-controller/.cosign
                    # location — else a missing config file denies all chutes-namespace images.
                    public_key=Path("/run/chutes/signing-keys/cosign/chutes.pub"),
                )
            ]

    def get_pin_ttl(self, image_no_tag: str) -> Optional[int]:
        """Return the TTL if *image_no_tag* is whitelisted for digest pinning, else ``None``.

        *image_no_tag* must be the fully-qualified image name without tag
        (e.g. ``docker.io/parachutes/failed-chute-cleanup``).
        """
        for entry in self.digest_pin_whitelist:
            if entry.image == image_no_tag:
                return entry.ttl
        return None

    def get_verification_config(
        self, registry: str, organization: str = "", repository: str = ""
    ) -> Optional[CosignVerificationConfig]:
        """
        Get the most specific verification config for an image.

        Precedence (most specific wins):
        1. Repository-level config (if organization and repository specified)
        2. Organization-level config (if organization specified)
        3. Registry-level config (default for that registry)
        4. Wildcard registry (*) config

        Args:
            registry: Registry hostname (e.g., "docker.io", "gcr.io")
            organization: Optional organization/namespace (e.g., "parachutes", "library")
            repository: Optional repository name (e.g., "chutes-agent", "nginx")

        Returns:
            The most specific CosignVerificationConfig, or None if no config found

        Examples:
            # Get registry-level config
            config.get_verification_config("docker.io")

            # Get org-level config
            config.get_verification_config("docker.io", "parachutes")

            # Get repo-level config
            config.get_verification_config("docker.io", "parachutes", "chutes-agent")
        """
        # Normalize registry name
        registry = self._normalize_registry_name(registry)

        logger.debug(f"Looking up config for {registry}/{organization}/{repository}")

        # Find matching registry config (exact match or pattern match)
        registry_config = None
        wildcard_config = None

        for config in self.registry_configs:
            if config.registry == registry:
                registry_config = config
                break
            elif config.registry == "*":
                wildcard_config = config
            elif self._matches_registry_pattern(registry, config.registry):
                registry_config = config
                break

        # Fall back to wildcard if no specific registry found
        if not registry_config:
            registry_config = wildcard_config

        if not registry_config:
            logger.warning(f"No registry config found for {registry}")
            return None

        # Start with registry-level defaults
        verification_config: CosignVerificationConfig = registry_config

        # Check for organization-level override
        org_config = None
        if registry_config.organizations:
            # Try exact match first
            if organization in registry_config.organizations:
                org_config = registry_config.organizations[organization]
            else:
                # Try pattern matching
                for org_pattern, org_cfg in registry_config.organizations.items():
                    if self._matches_pattern(organization, org_pattern):
                        org_config = org_cfg
                        break

            if org_config:
                logger.debug(f"Found org-level config for {organization}")
                verification_config = org_config

                # Check for repository-level override within this org
                if org_config.repositories:
                    # Try exact match first
                    if repository in org_config.repositories:
                        repo_config = org_config.repositories[repository]
                        logger.debug(f"Found repo-level config for {repository}")
                        verification_config = repo_config
                    else:
                        # Try pattern matching
                        for repo_pattern, repo_cfg in org_config.repositories.items():
                            if self._matches_pattern(repository, repo_pattern):
                                logger.debug(
                                    f"Found repo-level config for {repository} via pattern {repo_pattern}"
                                )
                                verification_config = repo_cfg
                                break

        logger.debug(
            f"Using {verification_config.__class__.__name__} for {registry}/{organization}/{repository}: "
            f"method={verification_config.verification_method}, "
            f"require_signature={verification_config.require_signature}"
        )

        return verification_config

    def _normalize_registry_name(self, registry: str) -> str:
        """Normalize registry name for consistent matching."""
        # Remove protocol if present
        if registry.startswith(("http://", "https://")):
            registry = urlparse(registry).netloc

        # Handle Docker Hub special cases
        if registry in ["docker.io", "registry-1.docker.io", "index.docker.io"]:
            return "docker.io"

        return registry.lower()

    def _matches_registry_pattern(self, registry: str, pattern: str) -> bool:
        """Match registry against a pattern.

        Supports:
        - Exact match: "docker.io" matches "docker.io"
        - Prefix with wildcard: "docker.io/*" matches "docker.io"
        - Prefix match: "gcr.io*" matches "gcr.io", "gcr.io.local", etc.
        - Suffix match: "*.gcr.io" matches "us.gcr.io", "eu.gcr.io", etc.
        - Wildcard: "*" matches everything
        """
        if pattern == "*":
            return True

        registry = registry.lower()
        pattern = pattern.lower()

        # Simple wildcard support
        if "*" in pattern:
            if pattern.endswith("/*"):
                # Prefix with slash: "docker.io/*" matches "docker.io"
                prefix = pattern[:-2]
                return registry.startswith(prefix + "/") or registry == prefix
            elif pattern.endswith("*"):
                # Prefix match: gcr.io* matches gcr.io, gcr.io.local, etc.
                prefix = pattern[:-1]
                return registry.startswith(prefix)
            elif pattern.startswith("*"):
                # Suffix match: *.gcr.io matches us.gcr.io, eu.gcr.io, etc.
                suffix = pattern[1:]
                return registry.endswith(suffix)

        return registry == pattern

    def _matches_pattern(self, value: str, pattern: str) -> bool:
        """
        Simple pattern matching for organizations and repositories.

        Supports:
        - Exact match: "parachutes" matches "parachutes"
        - Prefix match: "google/*" matches "google/anything"
        - Suffix match: "*/base" matches "anything/base"
        - Wildcard: "*" matches everything
        """
        if pattern == "*":
            return True

        value = value.lower()
        pattern = pattern.lower()

        if "*" not in pattern:
            # Exact match
            return value == pattern

        if pattern.endswith("/*"):
            # Prefix match: "google/*" matches "google/cloud-sdk", "google/anything"
            prefix = pattern[:-2]
            return value.startswith(prefix + "/") or value == prefix

        if pattern.startswith("*/"):
            # Suffix match: "*/base" matches "distroless/base", "alpine/base"
            suffix = pattern[2:]
            return value.endswith("/" + suffix) or value == suffix

        if pattern == "*/*":
            # Match anything with at least one slash
            return "/" in value

        # For more complex patterns, you could use fnmatch or regex
        # For now, just do simple wildcard replacement
        import fnmatch

        return fnmatch.fnmatch(value, pattern)


class Validator(BaseModel):
    hotkey: str
    registry: str
    api: str
    socket: str


class MinerConfig(BaseSettings):
    """
    Miner credentials for signing requests to validators (e.g. cache hf_info).
    Optional: when MINER_SS58/MINER_SEED are not set, signing is unavailable.
    """

    miner_ss58: Optional[str] = Field(default=None, alias="MINER_SS58")
    miner_seed: Optional[str] = Field(default=None, alias="MINER_SEED")
    validators_json: Optional[str] = Field(default=None, alias="VALIDATORS")

    _validators: List[Validator] = PrivateAttr(default=[])
    _keypair: Optional[Keypair] = PrivateAttr(default=None)

    model_config = SettingsConfigDict(
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    @property
    def miner_keypair(self) -> Optional[Keypair]:
        """Keypair for signing; built from miner_seed when both ss58 and seed are set."""
        if self._keypair is None:
            self._keypair = Keypair.create_from_seed(self.miner_seed)
        return self._keypair

    @property
    def validators(self) -> List[Validator]:
        if self._validators:
            return self._validators
        if self.validators_json is None:
            raise ValueError("VALIDATORS env var must be set")
        data = json.loads(self.validators_json)
        self._validators = [Validator(**item) for item in data["supported"]]
        return self._validators


# For backward compatibility and convenience
def load_config(**kwargs) -> AdmissionConfig:
    """Load configuration with environment variables and optional overrides."""
    return AdmissionConfig(**kwargs)
