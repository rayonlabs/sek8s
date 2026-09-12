# tests/unit/test_validators.py
"""
Unit tests for individual validators
"""

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from sek8s.clients.cosign import (
    CosignRateLimitError,
    CosignVerificationUnavailableError,
)
from sek8s.config import (
    AdmissionConfig,
    CosignConfig,
    CosignVerificationConfig,
    NamespacePolicy,
)
from sek8s.validators.base import ValidationResult
from sek8s.validators.cosign import CosignValidator, RateLimitError
from sek8s.validators.opa import OPAValidator
from sek8s.validators.registry import RegistryValidator


@pytest.fixture
def config():
    """Create test configuration."""
    # Aliased pydantic-settings fields must be constructed via their env alias;
    # field-name kwargs are silently ignored (see no-populate-by-name rationale).
    return AdmissionConfig(
        OPA_URL="http://localhost:8181",
        OPA_TIMEOUT=5.0,
        ALLOWED_REGISTRIES=["docker.io", "gcr.io", "quay.io", "registry.chutes.ai"],
        ENFORCEMENT_MODE="enforce",
    )


class TestRegistryValidator:
    """Tests for RegistryValidator."""

    @pytest.mark.asyncio
    async def test_allowed_registry(self, config, valid_admission_review):
        """Test that allowed registries pass validation."""
        validator = RegistryValidator(config)
        result = await validator.validate(valid_admission_review)

        assert result.allowed is True
        assert len(result.messages) == 0

    @pytest.mark.asyncio
    async def test_disallowed_registry(self, config, untrusted_registry_review):
        """Test that disallowed registries are rejected."""
        validator = RegistryValidator(config)
        result = await validator.validate(untrusted_registry_review)

        assert result.allowed is False
        assert "disallowed registry" in result.messages[0]
        assert "untrusted-registry.com" in result.messages[0]

    @pytest.mark.asyncio
    async def test_docker_hub_short_form(self, config):
        """Test Docker Hub short form images (library/nginx)."""
        review = {
            "request": {
                "kind": {"kind": "Pod"},
                "namespace": "default",
                "object": {
                    "spec": {
                        "containers": [
                            {"image": "nginx:latest"}  # Docker Hub short form
                        ]
                    }
                },
            }
        }

        validator = RegistryValidator(config)
        result = await validator.validate(review)

        assert result.allowed is True

    @pytest.mark.asyncio
    async def test_non_pod_resource_skipped(self, config, service_review):
        """Test that non-pod resources are skipped."""
        validator = RegistryValidator(config)
        result = await validator.validate(service_review)

        assert result.allowed is True

    @pytest.mark.asyncio
    async def test_deployment_template_validation(self, config, deployment_review):
        """Test that deployments are validated."""
        validator = RegistryValidator(config)
        result = await validator.validate(deployment_review)

        assert result.allowed is True

    @pytest.mark.asyncio
    async def test_exempt_namespace(self, config):
        """Test that exempt namespaces are handled correctly."""
        config.namespace_policies["test-exempt"] = NamespacePolicy(
            mode="warn", exempt=True
        )

        review = {
            "request": {
                "kind": {"kind": "Pod"},
                "namespace": "test-exempt",
                "object": {
                    "spec": {
                        "containers": [{"image": "untrusted-registry.com/app:latest"}]
                    }
                },
            }
        }

        validator = RegistryValidator(config)
        result = await validator.validate(review)

        assert result.allowed is True
        assert "exempt" in result.messages[0] if result.messages else True

    @pytest.mark.asyncio
    async def test_monitor_mode(self, config):
        """Test monitor mode allows but warns."""
        config.namespace_policies["default"].mode = "monitor"

        review = {
            "request": {
                "kind": {"kind": "Pod"},
                "namespace": "default",
                "object": {
                    "spec": {
                        "containers": [{"image": "untrusted-registry.com/app:latest"}]
                    }
                },
            }
        }

        validator = RegistryValidator(config)
        result = await validator.validate(review)

        assert result.allowed is True
        assert len(result.warnings) > 0
        assert "monitor mode" in result.warnings[0]


class TestOPAValidator:
    """Tests for OPAValidator."""

    @pytest.mark.asyncio
    async def test_opa_allow(
        self, config, valid_admission_review, mock_aiohttp_session
    ):
        """Test OPA validator when OPA allows the request."""
        validator = OPAValidator(config)

        mock_response = AsyncMock()
        mock_response.status = 200
        mock_response.json = AsyncMock(return_value={"result": []})

        mock_session = mock_aiohttp_session(mock_response)

        validator.session = mock_session

        result = await validator.validate(valid_admission_review)

        assert result.allowed is True

    @pytest.mark.asyncio
    async def test_opa_deny_with_violations(
        self, config, privileged_pod_review, mock_aiohttp_session
    ):
        """Test OPA validator when OPA denies with violations."""
        validator = OPAValidator(config)

        mock_response = AsyncMock()
        mock_response.status = 200
        mock_response.json = AsyncMock(
            return_value={
                "result": [
                    {"msg": "Container 'app' has privileged security context"},
                    "Direct string violation",
                ]
            }
        )

        mock_session = mock_aiohttp_session(mock_response)

        validator.session = mock_session

        result = await validator.validate(privileged_pod_review)

        assert result.allowed is False
        assert "privileged security context" in result.messages[0]

    @pytest.mark.asyncio
    async def test_opa_timeout(
        self, config, valid_admission_review, mock_aiohttp_session
    ):
        """Test OPA validator handles timeout (fail closed)."""
        validator = OPAValidator(config)

        mock_session = mock_aiohttp_session(None)
        mock_session.post.return_value.__aenter__ = AsyncMock(
            side_effect=asyncio.TimeoutError()
        )

        validator.session = mock_session

        result = await validator.validate(valid_admission_review)

        assert result.allowed is False
        assert "timeout" in result.messages[0].lower()

    @pytest.mark.asyncio
    async def test_opa_error_response(
        self, config, valid_admission_review, mock_aiohttp_session
    ):
        """Test OPA validator handles error responses."""
        validator = OPAValidator(config)

        mock_response = AsyncMock()
        mock_response.status = 500
        mock_session = mock_aiohttp_session(mock_response)

        validator.session = mock_session

        result = await validator.validate(valid_admission_review)

        assert result.allowed is False
        assert "OPA returned status 500" in result.messages[0]

    @pytest.mark.asyncio
    async def test_opa_health_check_success(self, config, mock_aiohttp_session):
        """Test OPA health check when healthy."""
        validator = OPAValidator(config)

        mock_response = AsyncMock()
        mock_response.status = 200
        mock_session = mock_aiohttp_session(mock_response)

        validator.session = mock_session

        is_healthy = await validator.health_check()

        assert is_healthy is True

    @pytest.mark.asyncio
    async def test_opa_health_check_failure(self, config, mock_aiohttp_session):
        """Test OPA health check when unhealthy."""
        validator = OPAValidator(config)

        mock_session = mock_aiohttp_session(None)
        mock_session.get.return_value.__aenter__ = AsyncMock(
            side_effect=Exception("Connection failed")
        )

        validator.session = mock_session

        is_healthy = await validator.health_check()

        assert is_healthy is False

    @pytest.mark.asyncio
    async def test_opa_warn_mode(
        self, config, privileged_pod_review, mock_aiohttp_session
    ):
        """Test OPA validator in warn mode."""
        config.namespace_policies["default"].mode = "warn"
        validator = OPAValidator(config)

        mock_response = AsyncMock()
        mock_response.status = 200
        mock_response.json = AsyncMock(
            return_value={"result": [{"msg": "Policy violation"}]}
        )
        mock_session = mock_aiohttp_session(mock_response)

        validator.session = mock_session

        result = await validator.validate(privileged_pod_review)

        assert result.allowed is True
        assert len(result.warnings) > 0
        assert "Policy violations detected" in result.warnings[0]


class TestValidationResult:
    """Tests for ValidationResult class."""

    def test_allow_result(self):
        """Test creating an allow result."""
        result = ValidationResult.allow("Success message", "Warning message")

        assert result.allowed is True
        assert "Success message" in result.messages
        assert "Warning message" in result.warnings

    def test_deny_result(self):
        """Test creating a deny result."""
        result = ValidationResult.deny("Denial reason")

        assert result.allowed is False
        assert "Denial reason" in result.messages
        assert len(result.warnings) == 0

    def test_combine_results_all_allowed(self):
        """Test combining multiple allowed results."""
        results = [
            ValidationResult.allow("Message 1", "Warning 1"),
            ValidationResult.allow("Message 2", "Warning 2"),
        ]

        combined = ValidationResult.combine(results)

        assert combined.allowed is True
        assert len(combined.messages) == 2
        assert len(combined.warnings) == 2

    def test_combine_results_with_denial(self):
        """Test combining results with at least one denial."""
        results = [
            ValidationResult.allow("Allowed"),
            ValidationResult.deny("Denied"),
            ValidationResult.allow(warning="Warning"),
        ]

        combined = ValidationResult.combine(results)

        assert combined.allowed is False
        assert "Denied" in combined.messages
        assert "Warning" in combined.warnings


class TestCosignValidator:
    """Tests for CosignValidator."""

    def test_normalize_registry_hostname(self):
        """Test registry hostname lowercasing for ctr/registries.yaml match."""
        from sek8s.image_utils import normalize_registry_hostname

        assert (
            normalize_registry_hostname("REGISTRY.CHUTES.AI/chutes/sglang:tag")
            == "registry.chutes.ai/chutes/sglang:tag"
        )
        assert normalize_registry_hostname("nginx:latest") == "nginx:latest"
        assert (
            normalize_registry_hostname("localhost:30500/org/image:tag")
            == "localhost:30500/org/image:tag"
        )

    def test_parse_image_reference_docker_hub_with_org(self):
        """Test parsing Docker Hub image with organization."""
        from sek8s.image_utils import parse_image_reference

        registry, org, repo, tag = parse_image_reference(
            "parachutes/chutes-agent:k3s-latest"
        )

        assert registry == "docker.io"
        assert org == "parachutes"
        assert repo == "chutes-agent"
        assert tag == "k3s-latest"

    def test_parse_image_reference_official_image(self):
        """Test parsing Docker Hub official image."""
        from sek8s.image_utils import parse_image_reference

        registry, org, repo, tag = parse_image_reference("nginx:latest")

        assert registry == "docker.io"
        assert org == "library"
        assert repo == "nginx"
        assert tag == "latest"

    def test_parse_image_reference_with_registry(self):
        """Test parsing image with explicit registry."""
        from sek8s.image_utils import parse_image_reference

        registry, org, repo, tag = parse_image_reference(
            "gcr.io/google-containers/pause:3.9"
        )

        assert registry == "gcr.io"
        assert org == "google-containers"
        assert repo == "pause"
        assert tag == "3.9"

    def test_parse_image_reference_with_digest(self):
        """Test parsing image with digest."""
        from sek8s.image_utils import parse_image_reference

        registry, org, repo, tag = parse_image_reference(
            "docker.io/parachutes/app@sha256:abcd1234"
        )

        assert registry == "docker.io"
        assert org == "parachutes"
        assert repo == "app"
        assert tag == "@sha256:abcd1234"

    def test_is_digest_pinned_reference(self):
        """Digest-pinned refs are detectable; tag-only refs are not."""
        from sek8s.image_utils import is_digest_pinned_reference

        assert is_digest_pinned_reference(
            "docker.io/library/nginx@sha256:abcdef1234567890abcdef1234567890abcdef1234567890abcdef1234567890"
        )
        assert is_digest_pinned_reference("gcr.io/p/foo/bar@sha512:abcd")
        assert not is_digest_pinned_reference("docker.io/library/nginx:latest")
        assert not is_digest_pinned_reference("nginx")

    @pytest.mark.asyncio
    async def test_digest_pinned_result_is_cached(self, config, tmp_path):
        """Digest-pinned image: first call verifies, second returns cached result."""
        key_file = tmp_path / "cosign.pub"
        key_file.write_text("test")
        vc = CosignVerificationConfig(
            verification_method="key",
            public_key=key_file,
            rekor_url="https://rekor.sigstore.dev",
        )
        validator = CosignValidator(config)
        calls = []

        async def count_verify(*args, **kwargs):
            calls.append(1)
            return (True, None)

        digest_img = "docker.io/test/img@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
        with patch.object(validator._cosign_client, "verify", side_effect=count_verify):
            assert await validator._verify_image_signature(digest_img, vc) is True
            assert await validator._verify_image_signature(digest_img, vc) is True
        assert len(calls) == 1

    @pytest.mark.asyncio
    async def test_tag_only_always_re_verifies(self, config, tmp_path):
        """Tag-only success: never cached; each sequential admission calls cosign."""
        key_file = tmp_path / "cosign.pub"
        key_file.write_text("test")
        vc = CosignVerificationConfig(
            verification_method="key",
            public_key=key_file,
            rekor_url="https://rekor.sigstore.dev",
        )
        validator = CosignValidator(config)
        calls = []

        async def count_verify(*args, **kwargs):
            calls.append(1)
            return (True, None)

        with patch.object(validator._cosign_client, "verify", side_effect=count_verify):
            await validator._verify_image_signature("docker.io/test/img:latest", vc)
            await validator._verify_image_signature("docker.io/test/img:latest", vc)
        assert len(calls) == 2

    @pytest.mark.asyncio
    async def test_tag_failure_is_cached(self, config, tmp_path):
        """Invalid tag-only verify cached so kube retries do not hammer the registry."""
        key_file = tmp_path / "cosign.pub"
        key_file.write_text("test")
        vc = CosignVerificationConfig(
            verification_method="key",
            public_key=key_file,
            rekor_url="https://rekor.sigstore.dev",
        )
        validator = CosignValidator(config)
        validator.cosign_config = CosignConfig(tag_failure_cache_ttl_seconds=300)
        calls = []

        async def fail_verify(*args, **kwargs):
            calls.append(1)
            return (False, None)

        tag_img = "docker.io/test/img:latest"
        with patch.object(validator._cosign_client, "verify", side_effect=fail_verify):
            assert await validator._verify_image_signature(tag_img, vc) is False
            assert await validator._verify_image_signature(tag_img, vc) is False
        assert len(calls) == 1

    @pytest.mark.asyncio
    async def test_tag_failure_cache_respects_zero_ttl(self, config, tmp_path):
        """tag_failure_cache_ttl_seconds=0 disables caching invalid tag signatures."""
        key_file = tmp_path / "cosign.pub"
        key_file.write_text("test")
        vc = CosignVerificationConfig(
            verification_method="key",
            public_key=key_file,
            rekor_url="https://rekor.sigstore.dev",
        )
        validator = CosignValidator(config)
        validator.cosign_config = CosignConfig(tag_failure_cache_ttl_seconds=0)
        calls = []

        async def fail_verify(*args, **kwargs):
            calls.append(1)
            return (False, None)

        tag_img = "docker.io/test/img:latest"
        with patch.object(validator._cosign_client, "verify", side_effect=fail_verify):
            assert await validator._verify_image_signature(tag_img, vc) is False
            assert await validator._verify_image_signature(tag_img, vc) is False
        assert len(calls) == 2

    @pytest.mark.asyncio
    async def test_failure_is_cached_for_digest(self, config, tmp_path):
        """Invalid signature cached so we don't re-verify the same bad image."""
        key_file = tmp_path / "cosign.pub"
        key_file.write_text("test")
        vc = CosignVerificationConfig(
            verification_method="key",
            public_key=key_file,
            rekor_url="https://rekor.sigstore.dev",
        )
        validator = CosignValidator(config)
        calls = []

        async def fail_verify(*args, **kwargs):
            calls.append(1)
            return (False, None)

        digest_img = "docker.io/test/img@sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
        with patch.object(validator._cosign_client, "verify", side_effect=fail_verify):
            assert await validator._verify_image_signature(digest_img, vc) is False
            assert await validator._verify_image_signature(digest_img, vc) is False
        assert len(calls) == 1

    @pytest.mark.asyncio
    async def test_transient_error_cached_short_ttl(self, config, tmp_path):
        """Transient errors (network) are cached briefly so we don't spam the endpoint."""
        key_file = tmp_path / "cosign.pub"
        key_file.write_text("test")
        vc = CosignVerificationConfig(
            verification_method="key",
            public_key=key_file,
            rekor_url="https://rekor.sigstore.dev",
        )
        validator = CosignValidator(config)
        calls = []

        async def fail_transient(*args, **kwargs):
            calls.append(1)
            raise CosignVerificationUnavailableError("connection refused")

        digest_img = "docker.io/test/img@sha256:cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc"
        with patch.object(
            validator._cosign_client, "verify", side_effect=fail_transient
        ):
            with pytest.raises(CosignVerificationUnavailableError):
                await validator._verify_image_signature(digest_img, vc)
            with pytest.raises(CosignVerificationUnavailableError):
                await validator._verify_image_signature(digest_img, vc)
        assert len(calls) == 1

    @pytest.mark.asyncio
    async def test_transient_error_cached_for_tag(self, config, tmp_path):
        """Transient errors for tag-only refs use short TTL like digest."""
        key_file = tmp_path / "cosign.pub"
        key_file.write_text("test")
        vc = CosignVerificationConfig(
            verification_method="key",
            public_key=key_file,
            rekor_url="https://rekor.sigstore.dev",
        )
        validator = CosignValidator(config)
        calls = []

        async def fail_transient(*args, **kwargs):
            calls.append(1)
            raise CosignVerificationUnavailableError("connection refused")

        tag_img = "docker.io/test/img:latest"
        with patch.object(
            validator._cosign_client, "verify", side_effect=fail_transient
        ):
            with pytest.raises(CosignVerificationUnavailableError):
                await validator._verify_image_signature(tag_img, vc)
            with pytest.raises(CosignVerificationUnavailableError):
                await validator._verify_image_signature(tag_img, vc)
        assert len(calls) == 1

    @pytest.mark.asyncio
    async def test_rate_limit_sets_global_backoff(self, config, tmp_path):
        """Upstream 429 pauses all verifications, not just the triggering image."""
        key_file = tmp_path / "cosign.pub"
        key_file.write_text("test")
        vc = CosignVerificationConfig(
            verification_method="key",
            public_key=key_file,
            rekor_url="https://rekor.sigstore.dev",
        )
        validator = CosignValidator(config)

        async def raise_rl(*args, **kwargs):
            raise CosignRateLimitError("rate limited")

        with patch.object(validator._cosign_client, "verify", side_effect=raise_rl):
            with pytest.raises(CosignRateLimitError):
                await validator._verify_image_signature("docker.io/x:latest", vc)

        other_img = "docker.io/other@sha256:dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd"
        with pytest.raises(RateLimitError):
            await validator._verify_image_signature(other_img, vc)

    @pytest.mark.asyncio
    async def test_concurrent_same_digest_each_invokes_cosign(self, config, tmp_path):
        """No singleflight: concurrent verifies for the same ref each run cosign."""
        key_file = tmp_path / "cosign.pub"
        key_file.write_text("test")
        vc = CosignVerificationConfig(
            verification_method="key",
            public_key=key_file,
            rekor_url="https://rekor.sigstore.dev",
        )
        validator = CosignValidator(config)
        calls = []

        async def slow_verify(*args, **kwargs):
            calls.append(1)
            await asyncio.sleep(0.02)
            return (True, None)

        digest_img = "docker.io/test/img@sha256:eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee"
        with patch.object(validator._cosign_client, "verify", side_effect=slow_verify):
            results = await asyncio.gather(
                *[validator._verify_image_signature(digest_img, vc) for _ in range(8)]
            )

        assert all(r is True for r in results)
        assert len(calls) == 8

    @pytest.mark.asyncio
    async def test_concurrent_different_digests_parallel_cosign(self, config, tmp_path):
        """Different digest-pinned images verify concurrently (no global cosign lock)."""
        key_file = tmp_path / "cosign.pub"
        key_file.write_text("test")
        vc = CosignVerificationConfig(
            verification_method="key",
            public_key=key_file,
            rekor_url="https://rekor.sigstore.dev",
        )
        validator = CosignValidator(config)
        gate = asyncio.Event()
        calls = []

        async def slow_verify(*args, **kwargs):
            calls.append(1)
            await gate.wait()
            return (True, None)

        img_a = "docker.io/test/a@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
        img_b = "docker.io/test/b@sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
        with patch.object(validator._cosign_client, "verify", side_effect=slow_verify):
            t1 = asyncio.create_task(validator._verify_image_signature(img_a, vc))
            t2 = asyncio.create_task(validator._verify_image_signature(img_b, vc))
            await asyncio.sleep(0.05)
            assert len(calls) == 2
            gate.set()
            await asyncio.gather(t1, t2)
        assert len(calls) == 2
