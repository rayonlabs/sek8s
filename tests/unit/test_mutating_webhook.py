"""Unit tests for the mutating webhook digest-pinning feature.

Tests cover:
- _extract_verified_digest from cosign JSON output
- DigestPinEntry / CosignConfig.get_pin_ttl whitelist lookup
- _TagVerification cache + CosignValidator.get_pinned_digest
- CosignValidator populates tag cache only for whitelisted images
- AdmissionController.build_image_pin_patches JSON Patch generation
- strip_tag helper
"""

import json
import time
from unittest.mock import patch

import pytest

from sek8s.clients.cosign import _extract_verified_digest
from sek8s.config import (
    AdmissionConfig,
    CosignConfig,
    CosignVerificationConfig,
    DigestPinEntry,
)
from sek8s.image_utils import strip_tag
from sek8s.services.admission_controller import AdmissionController
from sek8s.validators.cosign import CosignValidator, _TagVerification

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def config():
    # Aliased pydantic-settings fields must be constructed via their env alias;
    # field-name kwargs are silently ignored (see no-populate-by-name rationale).
    return AdmissionConfig(
        OPA_URL="http://localhost:8181",
        OPA_TIMEOUT=5.0,
        ALLOWED_REGISTRIES=["docker.io", "gcr.io", "quay.io", "registry.chutes.ai"],
        ENFORCEMENT_MODE="enforce",
    )


# ---------------------------------------------------------------------------
# _extract_verified_digest
# ---------------------------------------------------------------------------


class TestExtractVerifiedDigest:
    def test_valid_cosign_output(self):
        stdout = json.dumps(
            [
                {
                    "critical": {
                        "identity": {"docker-reference": "docker.io/parachutes/foo"},
                        "image": {"docker-manifest-digest": "sha256:abcdef1234567890"},
                        "type": "cosign container image signature",
                    },
                    "optional": {},
                }
            ]
        )
        assert _extract_verified_digest(stdout) == "sha256:abcdef1234567890"

    def test_empty_array(self):
        assert _extract_verified_digest("[]") is None

    def test_invalid_json(self):
        assert _extract_verified_digest("not json") is None

    def test_missing_critical(self):
        assert _extract_verified_digest(json.dumps([{"optional": {}}])) is None

    def test_missing_image_key(self):
        stdout = json.dumps([{"critical": {"identity": {}}}])
        assert _extract_verified_digest(stdout) is None

    def test_empty_string(self):
        assert _extract_verified_digest("") is None

    def test_multiple_signatures_uses_first(self):
        stdout = json.dumps(
            [
                {"critical": {"image": {"docker-manifest-digest": "sha256:first"}}},
                {"critical": {"image": {"docker-manifest-digest": "sha256:second"}}},
            ]
        )
        assert _extract_verified_digest(stdout) == "sha256:first"


# ---------------------------------------------------------------------------
# strip_tag
# ---------------------------------------------------------------------------


class TestStripTag:
    def test_with_tag(self):
        assert (
            strip_tag("docker.io/parachutes/foo:latest") == "docker.io/parachutes/foo"
        )

    def test_with_digest(self):
        assert (
            strip_tag("docker.io/parachutes/foo@sha256:abc123")
            == "docker.io/parachutes/foo"
        )

    def test_short_form(self):
        assert strip_tag("parachutes/foo:v1.2") == "docker.io/parachutes/foo"

    def test_bare_image(self):
        assert strip_tag("nginx:latest") == "docker.io/library/nginx"

    def test_no_tag(self):
        assert strip_tag("nginx") == "docker.io/library/nginx"


# ---------------------------------------------------------------------------
# DigestPinEntry / CosignConfig.get_pin_ttl
# ---------------------------------------------------------------------------


class TestDigestPinWhitelist:
    def test_get_pin_ttl_match(self):
        cfg = CosignConfig(
            digest_pin_whitelist=[
                DigestPinEntry(
                    image="docker.io/parachutes/failed-chute-cleanup", ttl=1800
                ),
            ]
        )
        assert cfg.get_pin_ttl("docker.io/parachutes/failed-chute-cleanup") == 1800

    def test_get_pin_ttl_no_match(self):
        cfg = CosignConfig(
            digest_pin_whitelist=[
                DigestPinEntry(
                    image="docker.io/parachutes/failed-chute-cleanup", ttl=1800
                ),
            ]
        )
        assert cfg.get_pin_ttl("docker.io/parachutes/other-image") is None

    def test_get_pin_ttl_empty_whitelist(self):
        cfg = CosignConfig()
        assert cfg.get_pin_ttl("docker.io/parachutes/anything") is None

    def test_default_ttl(self):
        entry = DigestPinEntry(image="docker.io/parachutes/foo")
        assert entry.ttl == 3600


# ---------------------------------------------------------------------------
# _TagVerification
# ---------------------------------------------------------------------------


class TestTagVerification:
    def test_not_expired(self):
        tv = _TagVerification(
            digest="sha256:abc",
            verified_at=time.monotonic(),
            ttl=3600.0,
        )
        assert not tv.expired

    def test_expired(self):
        tv = _TagVerification(
            digest="sha256:abc",
            verified_at=time.monotonic() - 7200,
            ttl=3600.0,
        )
        assert tv.expired


# ---------------------------------------------------------------------------
# CosignValidator.get_pinned_digest
# ---------------------------------------------------------------------------


class TestGetPinnedDigest:
    def test_returns_digest_when_cached(self, config):
        validator = CosignValidator(config)
        validator._tag_cache["docker.io/parachutes/foo:latest"] = _TagVerification(
            digest="sha256:aaa",
            verified_at=time.monotonic(),
            ttl=3600.0,
        )
        assert (
            validator.get_pinned_digest("docker.io/parachutes/foo:latest")
            == "sha256:aaa"
        )

    def test_returns_none_when_expired(self, config):
        validator = CosignValidator(config)
        validator._tag_cache["docker.io/parachutes/foo:latest"] = _TagVerification(
            digest="sha256:aaa",
            verified_at=time.monotonic() - 7200,
            ttl=3600.0,
        )
        assert validator.get_pinned_digest("docker.io/parachutes/foo:latest") is None

    def test_returns_none_when_not_cached(self, config):
        validator = CosignValidator(config)
        assert validator.get_pinned_digest("docker.io/parachutes/foo:latest") is None


# ---------------------------------------------------------------------------
# CosignValidator._verify_image_signature populates tag cache for whitelisted
# ---------------------------------------------------------------------------


class TestVerifyPopulatesTagCache:
    @pytest.mark.asyncio
    async def test_whitelisted_tag_populates_cache(self, config, tmp_path):
        key_file = tmp_path / "cosign.pub"
        key_file.write_text("test")
        vc = CosignVerificationConfig(
            verification_method="key",
            public_key=key_file,
            rekor_url="https://rekor.sigstore.dev",
        )
        validator = CosignValidator(config)
        validator.cosign_config = CosignConfig(
            digest_pin_whitelist=[
                DigestPinEntry(image="docker.io/parachutes/foo", ttl=1800),
            ]
        )

        async def mock_verify(*args, **kwargs):
            return (True, "sha256:deadbeef")

        image = "docker.io/parachutes/foo:latest"
        with patch.object(validator._cosign_client, "verify", side_effect=mock_verify):
            result = await validator._verify_image_signature(image, vc)

        assert result is True
        assert validator.get_pinned_digest(image) == "sha256:deadbeef"
        assert "docker.io/parachutes/foo@sha256:deadbeef" in validator._cache

    @pytest.mark.asyncio
    async def test_non_whitelisted_tag_does_not_populate_cache(self, config, tmp_path):
        key_file = tmp_path / "cosign.pub"
        key_file.write_text("test")
        vc = CosignVerificationConfig(
            verification_method="key",
            public_key=key_file,
            rekor_url="https://rekor.sigstore.dev",
        )
        validator = CosignValidator(config)
        validator.cosign_config = CosignConfig(digest_pin_whitelist=[])

        async def mock_verify(*args, **kwargs):
            return (True, "sha256:deadbeef")

        image = "docker.io/parachutes/foo:latest"
        with patch.object(validator._cosign_client, "verify", side_effect=mock_verify):
            result = await validator._verify_image_signature(image, vc)

        assert result is True
        assert validator.get_pinned_digest(image) is None

    @pytest.mark.asyncio
    async def test_failed_verify_does_not_populate_tag_cache(self, config, tmp_path):
        key_file = tmp_path / "cosign.pub"
        key_file.write_text("test")
        vc = CosignVerificationConfig(
            verification_method="key",
            public_key=key_file,
            rekor_url="https://rekor.sigstore.dev",
        )
        validator = CosignValidator(config)
        validator.cosign_config = CosignConfig(
            digest_pin_whitelist=[
                DigestPinEntry(image="docker.io/parachutes/foo", ttl=1800),
            ]
        )

        async def mock_verify(*args, **kwargs):
            return (False, None)

        image = "docker.io/parachutes/foo:latest"
        with patch.object(validator._cosign_client, "verify", side_effect=mock_verify):
            result = await validator._verify_image_signature(image, vc)

        assert result is False
        assert validator.get_pinned_digest(image) is None


# ---------------------------------------------------------------------------
# AdmissionController.build_image_pin_patches
# ---------------------------------------------------------------------------


class TestBuildImagePinPatches:
    def test_pod_containers_pinned(self, config):
        controller = AdmissionController(config)
        controller._cosign_validator._tag_cache["docker.io/parachutes/foo:latest"] = (
            _TagVerification(
                digest="sha256:aaa",
                verified_at=time.monotonic(),
                ttl=3600.0,
            )
        )
        req = {
            "object": {
                "spec": {
                    "containers": [
                        {"name": "main", "image": "docker.io/parachutes/foo:latest"},
                    ]
                }
            }
        }
        patches = controller.build_image_pin_patches(req)
        assert len(patches) == 1
        assert patches[0]["op"] == "replace"
        assert patches[0]["path"] == "/spec/containers/0/image"
        assert patches[0]["value"] == "docker.io/parachutes/foo@sha256:aaa"

    def test_no_patch_for_non_whitelisted(self, config):
        controller = AdmissionController(config)
        req = {
            "object": {
                "spec": {
                    "containers": [
                        {"name": "main", "image": "docker.io/parachutes/bar:latest"},
                    ]
                }
            }
        }
        patches = controller.build_image_pin_patches(req)
        assert patches == []

    def test_no_patch_for_digest_pinned(self, config):
        controller = AdmissionController(config)
        req = {
            "object": {
                "spec": {
                    "containers": [
                        {
                            "name": "main",
                            "image": "docker.io/parachutes/foo@sha256:already",
                        },
                    ]
                }
            }
        }
        patches = controller.build_image_pin_patches(req)
        assert patches == []

    def test_deployment_template_containers(self, config):
        controller = AdmissionController(config)
        controller._cosign_validator._tag_cache["docker.io/parachutes/foo:v1"] = (
            _TagVerification(
                digest="sha256:bbb",
                verified_at=time.monotonic(),
                ttl=3600.0,
            )
        )
        req = {
            "object": {
                "spec": {
                    "template": {
                        "spec": {
                            "containers": [
                                {"name": "app", "image": "docker.io/parachutes/foo:v1"},
                            ]
                        }
                    }
                }
            }
        }
        patches = controller.build_image_pin_patches(req)
        assert len(patches) == 1
        assert patches[0]["path"] == "/spec/template/spec/containers/0/image"

    def test_cronjob_nested_template(self, config):
        controller = AdmissionController(config)
        controller._cosign_validator._tag_cache[
            "docker.io/parachutes/cleanup:latest"
        ] = _TagVerification(
            digest="sha256:ccc",
            verified_at=time.monotonic(),
            ttl=3600.0,
        )
        req = {
            "object": {
                "spec": {
                    "jobTemplate": {
                        "spec": {
                            "template": {
                                "spec": {
                                    "containers": [
                                        {
                                            "name": "job",
                                            "image": "docker.io/parachutes/cleanup:latest",
                                        },
                                    ]
                                }
                            }
                        }
                    }
                }
            }
        }
        patches = controller.build_image_pin_patches(req)
        assert len(patches) == 1
        assert (
            patches[0]["path"]
            == "/spec/jobTemplate/spec/template/spec/containers/0/image"
        )
        assert patches[0]["value"] == "docker.io/parachutes/cleanup@sha256:ccc"

    def test_init_containers_pinned(self, config):
        controller = AdmissionController(config)
        controller._cosign_validator._tag_cache["docker.io/parachutes/init:v1"] = (
            _TagVerification(
                digest="sha256:ddd",
                verified_at=time.monotonic(),
                ttl=3600.0,
            )
        )
        req = {
            "object": {
                "spec": {
                    "initContainers": [
                        {"name": "init", "image": "docker.io/parachutes/init:v1"},
                    ],
                    "containers": [
                        {"name": "main", "image": "docker.io/other/image:latest"},
                    ],
                }
            }
        }
        patches = controller.build_image_pin_patches(req)
        assert len(patches) == 1
        assert patches[0]["path"] == "/spec/initContainers/0/image"

    def test_mixed_pinned_and_unpinned(self, config):
        controller = AdmissionController(config)
        controller._cosign_validator._tag_cache["docker.io/parachutes/foo:latest"] = (
            _TagVerification(
                digest="sha256:eee",
                verified_at=time.monotonic(),
                ttl=3600.0,
            )
        )
        req = {
            "object": {
                "spec": {
                    "containers": [
                        {"name": "a", "image": "docker.io/parachutes/foo:latest"},
                        {"name": "b", "image": "docker.io/other/bar:latest"},
                    ]
                }
            }
        }
        patches = controller.build_image_pin_patches(req)
        assert len(patches) == 1
        assert patches[0]["path"] == "/spec/containers/0/image"

    def test_expired_cache_produces_no_patch(self, config):
        controller = AdmissionController(config)
        controller._cosign_validator._tag_cache["docker.io/parachutes/foo:latest"] = (
            _TagVerification(
                digest="sha256:old",
                verified_at=time.monotonic() - 7200,
                ttl=3600.0,
            )
        )
        req = {
            "object": {
                "spec": {
                    "containers": [
                        {"name": "main", "image": "docker.io/parachutes/foo:latest"},
                    ]
                }
            }
        }
        patches = controller.build_image_pin_patches(req)
        assert patches == []
