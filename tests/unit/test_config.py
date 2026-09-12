# tests/unit/test_config.py
"""
Unit tests for Pydantic configuration
"""

import json
import os
from pathlib import Path
from unittest.mock import patch

import pytest
from attestation_proxy.config import AttestationProxyConfig

from sek8s.config import (
    AdmissionConfig,
    CacheConfig,
    CosignConfig,
    NamespacePolicy,
    OPAConfig,
    load_config,
)


@pytest.fixture(autouse=True)
def test_env():
    """Fixture to set up a clean test environment with valid defaults.

    Saves the current environment, sets up test defaults, yields for test execution,
    then restores the original environment. Individual tests can override these
    defaults as needed.

    This fixture is automatically applied to all tests in this module.
    """
    # Save current environment
    original_env = os.environ.copy()

    # Clear test-related vars
    test_vars = [
        "BIND_ADDRESS",
        "PORT",
        "ADMISSION_BIND_ADDRESS",
        "ADMISSION_PORT",
        "TLS_CERT_PATH",
        "TLS_KEY_PATH",
        "OPA_URL",
        "ALLOWED_REGISTRIES",
        "NAMESPACE_POLICIES",
        "DEBUG",
        "ENFORCEMENT_MODE",
        "CACHE_TTL",
        "CACHE_MAXSIZE",
        "OIDC_IDENTITY_REGEX",
        "OIDC_ISSUER",
        "COSIGN_OIDC_IDENTITY_REGEX",
        "COSIGN_OIDC_ISSUER",
        "COSIGN_SUCCESS_CACHE_TTL",
        "COSIGN_FAILURE_CACHE_TTL",
        "COSIGN_TAG_FAILURE_CACHE_TTL",
        "POLICY_PATH",
    ]
    for var in test_vars:
        os.environ.pop(var, None)

    # Set valid test defaults that won't cause permission errors
    os.environ["POLICY_PATH"] = "/tmp/test_policies"

    yield

    # Restore original environment
    os.environ.clear()
    os.environ.update(original_env)


class TestAdmissionConfig:
    """Test AdmissionConfig with Pydantic v2 JSON format."""

    def test_default_config(self):
        """Test default configuration values."""
        config = AdmissionConfig()

        assert config.bind_address == "127.0.0.1"
        assert config.port == 8443
        assert config.allowed_registries == [
            "docker.io",
            "gcr.io",
            "quay.io",
            "registry.chutes.ai",
        ]
        assert config.enforcement_mode == "enforce"
        assert config.debug is False

    def test_default_namespace_policies_all_enforce(self):
        """All default namespace policies must use enforce mode.

        OPA user-based exemptions (is_system_or_controller_user) handle
        system controllers; namespace-level warn mode is a security bypass.
        """
        config = AdmissionConfig()

        for ns, policy in config.namespace_policies.items():
            assert policy.mode == "enforce", (
                f"Namespace '{ns}' has mode='{policy.mode}', expected 'enforce'. "
                "Warn mode bypasses OPA denial enforcement."
            )
            assert policy.exempt is False, (
                f"Namespace '{ns}' is exempt from admission control. "
                "Use OPA user-based exemptions instead."
            )

    def test_allowed_registries_json_parsing(self):
        """Test parsing of JSON array for allowed_registries."""
        # Set as JSON array (Pydantic v2 default behavior)
        os.environ["ALLOWED_REGISTRIES"] = '["docker.io", "gcr.io", "quay.io"]'

        config = AdmissionConfig()

        assert config.allowed_registries == ["docker.io", "gcr.io", "quay.io"]

    def test_allowed_registries_with_wildcards(self):
        """Test registry list with wildcards."""
        os.environ["ALLOWED_REGISTRIES"] = (
            '["docker.io", "*.amazonaws.com", "*.azurecr.io"]'
        )

        config = AdmissionConfig()

        assert config.allowed_registries == [
            "docker.io",
            "*.amazonaws.com",
            "*.azurecr.io",
        ]

    def test_namespace_policies_json_parsing(self):
        """Test parsing of JSON object for namespace_policies."""
        policies = {
            "kube-system": {"mode": "warn", "exempt": False},
            "production": {"mode": "enforce", "exempt": False},
            "development": {"mode": "monitor", "exempt": True},
        }

        os.environ["NAMESPACE_POLICIES"] = json.dumps(policies)

        config = AdmissionConfig()

        assert "kube-system" in config.namespace_policies
        assert config.namespace_policies["kube-system"].mode == "warn"
        assert config.namespace_policies["kube-system"].exempt is False

        assert "production" in config.namespace_policies
        assert config.namespace_policies["production"].mode == "enforce"

        assert "development" in config.namespace_policies
        assert config.namespace_policies["development"].exempt is True

    def test_boolean_parsing(self):
        """Test boolean environment variable parsing."""
        # Test various boolean representations
        for true_val in ["true", "True", "TRUE", "1"]:
            os.environ["DEBUG"] = true_val
            config = AdmissionConfig()
            assert config.debug is True

        for false_val in ["false", "False", "FALSE", "0"]:
            os.environ["DEBUG"] = false_val
            config = AdmissionConfig()
            assert config.debug is False

    def test_port_validation(self):
        """Test port range validation."""
        # Valid port
        os.environ["PORT"] = "9000"
        config = AdmissionConfig()
        assert config.port == 9000

        # Invalid port (too high)
        os.environ["PORT"] = "70000"
        with pytest.raises(ValueError):
            AdmissionConfig()

        # Invalid port (too low)
        os.environ["PORT"] = "0"
        with pytest.raises(ValueError):
            AdmissionConfig()

        # Clean up
        os.environ.pop("PORT", None)

    def test_enforcement_mode_validation(self):
        """Test enforcement mode enum validation."""
        # Valid modes
        for mode in ["enforce", "warn", "monitor"]:
            os.environ["ENFORCEMENT_MODE"] = mode
            config = AdmissionConfig()
            assert config.enforcement_mode == mode

        # Invalid mode
        os.environ["ENFORCEMENT_MODE"] = "invalid"
        with pytest.raises(ValueError):
            AdmissionConfig()

    def test_export_methods(self):
        """Test configuration export methods."""
        os.environ["ALLOWED_REGISTRIES"] = '["test.registry.com"]'
        os.environ["DEBUG"] = "true"

        config = AdmissionConfig()

        # Test JSON export
        json_str = config.export_json()
        parsed = json.loads(json_str)
        assert parsed["allowed_registries"] == ["test.registry.com"]
        assert parsed["debug"] is True

        # Test dict export
        dict_export = config.export_dict()
        assert dict_export["allowed_registries"] == ["test.registry.com"]
        assert dict_export["debug"] is True

    def test_get_namespace_policy(self):
        """Test getting namespace-specific policies."""
        policies = {
            "production": {"mode": "enforce", "exempt": False},
            "development": {"mode": "monitor", "exempt": True},
        }

        os.environ["NAMESPACE_POLICIES"] = json.dumps(policies)
        config = AdmissionConfig()

        # Get existing namespace
        prod_policy = config.get_namespace_policy("production")
        assert prod_policy.mode == "enforce"
        assert prod_policy.exempt is False

        # Get non-existent namespace (should return default)
        unknown_policy = config.get_namespace_policy("unknown")
        assert unknown_policy.mode == "enforce"  # default mode
        assert unknown_policy.exempt is False

        # Test is_namespace_exempt
        assert config.is_namespace_exempt("development") is True
        assert config.is_namespace_exempt("production") is False


class TestNamespacePolicy:
    """Tests for NamespacePolicy."""

    def test_default_namespace_policy(self):
        """Test default namespace policy values."""
        policy = NamespacePolicy()

        assert policy.mode == "enforce"
        assert policy.exempt is False

    def test_custom_namespace_policy(self):
        """Test custom namespace policy."""
        policy = NamespacePolicy(mode="warn", exempt=True)

        assert policy.mode == "warn"
        assert policy.exempt is True

    def test_invalid_mode(self):
        """Test invalid enforcement mode."""
        with pytest.raises(ValueError):
            NamespacePolicy(mode="invalid")


class TestOPAConfig:
    """Tests for OPAConfig."""

    def test_default_opa_config(self):
        """Test default OPA configuration."""
        config = OPAConfig()

        assert config.opa_binary_path == Path("/usr/local/bin/opa")
        assert config.opa_log_level == "info"
        assert config.opa_decision_logs is False
        assert not hasattr(
            config, "opa_diagnostic_addr"
        ), "diagnostic address removed — no production use case"

    def test_opa_config_env_override(self):
        """Test OPA config environment overrides."""
        os.environ["OPA_BINARY_PATH"] = "/custom/opa"
        os.environ["OPA_LOG_LEVEL"] = "debug"
        os.environ["OPA_DECISION_LOGS"] = "true"

        config = OPAConfig()

        assert config.opa_binary_path == Path("/custom/opa")
        assert config.opa_log_level == "debug"
        assert config.opa_decision_logs is True

        # Cleanup
        del os.environ["OPA_BINARY_PATH"]
        del os.environ["OPA_LOG_LEVEL"]
        del os.environ["OPA_DECISION_LOGS"]

    def test_invalid_log_level(self):
        """Test invalid OPA log level."""
        with pytest.raises(ValueError):
            OPAConfig(opa_log_level="invalid")


class TestCosignConfig:
    """Tests for CosignConfig."""

    def test_default_cosign_config(self):
        """Test default Cosign configuration."""
        config = CosignConfig()

        assert config.success_cache_ttl_seconds == 3600
        assert config.failure_cache_ttl_seconds == 600
        assert config.tag_failure_cache_ttl_seconds == 300
        assert config.oidc_identity_regex == "^https://github.com/your-org/.*"
        assert config.oidc_issuer == "https://token.actions.githubusercontent.com"
        assert config.cosign_rekor_url == "https://rekor.sigstore.dev"
        assert config.fulcio_url == "https://fulcio.sigstore.dev"

        # Should have exactly one default registry configuration
        assert len(config.registry_configs) == 1
        default_registry = config.registry_configs[0]
        assert default_registry.registry == "*"
        assert default_registry.require_signature is True
        assert default_registry.verification_method == "key"
        assert default_registry.public_key == Path(
            "/run/chutes/signing-keys/cosign/chutes.pub"
        )

    def test_cosign_config_from_env(self):
        """Test Cosign config with single COSIGN_* env alias per tunable."""
        os.environ["COSIGN_SUCCESS_CACHE_TTL"] = "7200"
        os.environ["COSIGN_FAILURE_CACHE_TTL"] = "120"
        os.environ["OIDC_IDENTITY_REGEX"] = "^https://github.com/myorg/.*"
        os.environ["OIDC_ISSUER"] = "https://custom.issuer.com"

        try:
            config = CosignConfig()

            assert config.success_cache_ttl_seconds == 7200
            assert config.failure_cache_ttl_seconds == 120
            assert config.oidc_identity_regex == "^https://github.com/myorg/.*"
            assert config.oidc_issuer == "https://custom.issuer.com"
        finally:
            for var in [
                "COSIGN_SUCCESS_CACHE_TTL",
                "COSIGN_FAILURE_CACHE_TTL",
                "OIDC_IDENTITY_REGEX",
                "OIDC_ISSUER",
            ]:
                os.environ.pop(var, None)

    def test_cosign_config_with_registry_configs_list(self):
        """Test Cosign config with registry configurations from list."""
        registry_configs = [
            {
                "registry": "localhost:5000",
                "require_signature": True,
                "verification_method": "key",
                "public_key": "/path/to/key.pub",
            },
            {
                "registry": "docker.io",
                "require_signature": False,
                "verification_method": "disabled",
            },
        ]

        with patch("pathlib.Path.exists", return_value=True):
            config = CosignConfig(registry_configs=registry_configs)

            assert len(config.registry_configs) == 2

            # Check first config
            local_config = config.registry_configs[0]
            assert local_config.registry == "localhost:5000"
            assert local_config.require_signature is True
            assert local_config.verification_method == "key"
            assert local_config.public_key == Path("/path/to/key.pub")

            # Check second config
            docker_config = config.registry_configs[1]
            assert docker_config.registry == "docker.io"
            assert docker_config.require_signature is False
            assert docker_config.verification_method == "disabled"

    def test_cosign_config_with_config_file(self):
        """Test Cosign config loading from JSON file."""
        import tempfile

        config_data = {
            "registries": [
                {
                    "registry": "gcr.io",
                    "require_signature": True,
                    "verification_method": "keyless",
                    "keyless_identity_regex": "^https://github.com/.*",
                    "keyless_issuer": "https://token.actions.githubusercontent.com",
                },
                {
                    "registry": "localhost:5000",
                    "require_signature": True,
                    "verification_method": "key",
                    "public_key": "/test/key.pub",
                },
            ]
        }

        with tempfile.NamedTemporaryFile(mode="w", suffix=".json") as fh:
            json.dump(config_data, fh)
            fh.flush()
            config_file = fh.name

            with patch("pathlib.Path.exists", return_value=True):
                config = CosignConfig(cosign_registries_file=Path(config_file))

                assert len(config.registry_configs) == 2

                # Check keyless config
                gcr_config = None
                for reg_config in config.registry_configs:
                    if reg_config.registry == "gcr.io":
                        gcr_config = reg_config
                        break

                assert gcr_config is not None
                assert gcr_config.verification_method == "keyless"
                assert gcr_config.keyless_identity_regex == "^https://github.com/.*"
                assert (
                    gcr_config.keyless_issuer
                    == "https://token.actions.githubusercontent.com"
                )

    def test_get_registry_config_exact_match(self):
        """Test getting registry config with exact match."""
        registry_configs = [
            {
                "registry": "gcr.io",
                "require_signature": True,
                "verification_method": "key",
                "public_key": "/gcr/key.pub",
            },
            {
                "registry": "docker.io",
                "require_signature": False,
                "verification_method": "disabled",
            },
        ]

        with patch("pathlib.Path.exists", return_value=True):
            config = CosignConfig(registry_configs=registry_configs)

            gcr_config = config.get_verification_config("gcr.io")
            assert gcr_config is not None
            assert gcr_config.registry == "gcr.io"
            assert gcr_config.verification_method == "key"

            docker_config = config.get_verification_config("docker.io")
            assert docker_config is not None
            assert docker_config.verification_method == "disabled"

    def test_get_registry_config_pattern_match(self):
        """Test getting registry config with pattern matching."""
        registry_configs = [
            {
                "registry": "docker.io/*",
                "require_signature": False,
                "verification_method": "disabled",
            },
            {
                "registry": "*",
                "require_signature": True,
                "verification_method": "key",
                "public_key": "/default/key.pub",
            },
        ]

        with patch("pathlib.Path.exists", return_value=True):
            config = CosignConfig(registry_configs=registry_configs)

            # Should match pattern
            docker_config = config.get_verification_config("docker.io")
            assert docker_config is not None
            assert docker_config.registry == "docker.io/*"

            # Should match wildcard
            unknown_config = config.get_verification_config("unknown.registry.com")
            assert unknown_config is not None
            assert unknown_config.registry == "*"

    def test_get_registry_config_no_match(self):
        """Test getting registry config when no match found."""
        registry_configs = [
            {
                "registry": "gcr.io",
                "require_signature": True,
                "verification_method": "key",
                "public_key": "/gcr/key.pub",
            }
        ]

        with patch("pathlib.Path.exists", return_value=True):
            config = CosignConfig(registry_configs=registry_configs)

            # No match should return None
            result = config.get_verification_config("docker.io")
            assert result is None

    def test_cosign_registry_config_validation(self):
        """Test CosignRegistryConfig validation."""
        from sek8s.config import CosignRegistryConfig

        # Test valid key-based config
        with patch("pathlib.Path.exists", return_value=True):
            config = CosignRegistryConfig(
                registry="localhost:5000",
                require_signature=True,
                verification_method="key",
                public_key="/path/to/key.pub",
            )
            assert config.verification_method == "key"
            assert config.public_key == Path("/path/to/key.pub")

        # Test keyless config
        config = CosignRegistryConfig(
            registry="gcr.io",
            require_signature=True,
            verification_method="keyless",
            keyless_identity_regex="^https://github.com/.*",
            keyless_issuer="https://token.actions.githubusercontent.com",
        )
        assert config.verification_method == "keyless"
        assert config.keyless_identity_regex == "^https://github.com/.*"

        # Test disabled config
        config = CosignRegistryConfig(
            registry="docker.io",
            require_signature=False,
            verification_method="disabled",
        )
        assert config.verification_method == "disabled"
        assert config.require_signature is False

    def test_invalid_verification_method(self):
        """Test invalid verification method."""
        from sek8s.config import CosignRegistryConfig

        with pytest.raises(ValueError):
            CosignRegistryConfig(
                registry="test.registry", verification_method="invalid"
            )


class TestLoadConfig:
    """Tests for load_config helper function."""

    def test_load_config_default(self):
        """Test load_config with defaults."""
        config = load_config()

        assert isinstance(config, AdmissionConfig)
        assert config.bind_address == "127.0.0.1"

    def test_load_config_with_overrides(self):
        """Test load_config with parameter overrides.

        Note: Due to Pydantic Settings source precedence, only fields without
        defaults can be overridden via kwargs. Fields like debug with default
        values cannot be overridden this way - use environment variables instead.
        """
        config = load_config(bind_address="0.0.0.0", port=9000)

        assert config.bind_address == "0.0.0.0"
        assert config.port == 9000


class TestProxyConfig:

    def test_allowed_validator(self):
        os.environ["ALLOWED_VALIDATORS"] = "abcd1234"
        config = AttestationProxyConfig()

        assert len(config.allowed_validators) == 1
        assert config.allowed_validators[0] == "abcd1234"

    def test_allowed_validators(self):
        os.environ["ALLOWED_VALIDATORS"] = "abcd1234,efgh6789"
        config = AttestationProxyConfig()

        assert len(config.allowed_validators) == 2
        assert config.allowed_validators[0] == "abcd1234"
        assert config.allowed_validators[1] == "efgh6789"

    def test_miner_ss58(self):
        os.environ["ALLOWED_VALIDATORS"] = "abcd1234,efgh6789"
        os.environ["MINER_SS58"] = "abcd1234"
        config = AttestationProxyConfig()

        assert config.miner_ss58 == "abcd1234"


class TestCacheConfig:
    """Tests for CacheConfig; module-level cache_config uses env, so tests mock env vars."""

    def test_cache_config_defaults(self):
        """CacheConfig uses defaults when env is not set."""
        os.environ.pop("HF_CACHE_BASE", None)
        os.environ.pop("VALIDATOR_BASE_URL", None)
        config = CacheConfig()
        assert config.cache_base == "/var/snap/cache"
        assert config.validator_base_url == "https://api.chutes.ai"

    def test_cache_config_from_env(self):
        """CacheConfig reads HF_CACHE_BASE and VALIDATOR_BASE_URL from env."""
        os.environ["HF_CACHE_BASE"] = "/tmp/test-cache"
        os.environ["VALIDATOR_BASE_URL"] = "https://validator.test"
        try:
            config = CacheConfig()
            assert config.cache_base == "/tmp/test-cache"
            assert config.validator_base_url == "https://validator.test"
        finally:
            for var in ["HF_CACHE_BASE", "VALIDATOR_BASE_URL"]:
                os.environ.pop(var, None)
