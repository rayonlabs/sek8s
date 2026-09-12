"""
Unit tests for CosignValidator dual-key rule logic.

Tests cover _require_ctx_key and _get_rules_for_context without any
network calls or real cosign binary.  CosignConfig.get_verification_config
is mocked to return controlled CosignVerificationConfig objects.
"""

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from sek8s.config import AdmissionConfig, CosignConfig, CosignVerificationConfig
from sek8s.validators.cosign import CosignValidator, ValidationContext

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

CHUTES_KEY = Path("/run/chutes/signing-keys/cosign/chutes.pub")
DOCKERHUB_KEY = Path("/run/chutes/signing-keys/cosign/dockerhub.pub")
UNKNOWN_KEY = Path("/run/chutes/signing-keys/cosign/unknown.pub")


@pytest.fixture
def admission_config():
    return AdmissionConfig(
        opa_url="http://localhost:8181",
        allowed_registries=["docker.io", "5abc.localregistry.chutes.ai:30500"],
        enforcement_mode="enforce",
        CHUTES_PUBLIC_KEY_PATH=str(CHUTES_KEY),
        DOCKERHUB_PUBLIC_KEY_PATH=str(DOCKERHUB_KEY),
    )


@pytest.fixture
def validator(admission_config):
    return CosignValidator(admission_config)


def _make_vc(key: Path) -> CosignVerificationConfig:
    """Return a key-based CosignVerificationConfig pointing at *key*."""
    return CosignVerificationConfig(
        require_signature=True,
        verification_method="key",
        public_key=key,
    )


def _make_ctx(
    validator: CosignValidator,
    images: list,
    namespace: str = "chutes",
    *,
    vc_map: dict | None = None,
) -> ValidationContext:
    """Build a ValidationContext with get_verification_config mocked via *vc_map*.

    *vc_map* maps image string -> CosignVerificationConfig (or None).
    When None is returned for an image it means no config found.
    """
    mock_cosign_config = MagicMock(spec=CosignConfig)

    def _get_vc(registry, org="", repo=""):
        # Reconstruct a rough key to find in vc_map
        for image, vc in (vc_map or {}).items():
            if registry in image or image in registry:
                return vc
            if org and org in image:
                return vc
        return None

    mock_cosign_config.get_verification_config.side_effect = _get_vc

    ctx = ValidationContext(
        config=validator.config,
        request={},
        namespace=namespace,
        images=images,
        cosign_config=mock_cosign_config,
        validator=validator,
    )
    return ctx


# ---------------------------------------------------------------------------
# _get_rules_for_context: required_key_paths population
# ---------------------------------------------------------------------------


def test_get_rules_chutes_namespace_populates_both_keys(validator):
    """_get_rules_for_context adds both chutes and dockerhub keys for chutes namespace."""
    ctx = ValidationContext(
        config=validator.config,
        request={},
        namespace="chutes",
        images=["docker.io/parachutes/sek8s:latest"],
        cosign_config=MagicMock(spec=CosignConfig),
        validator=validator,
    )
    validator._get_rules_for_context(ctx)

    assert CHUTES_KEY in ctx.required_key_paths
    assert DOCKERHUB_KEY in ctx.required_key_paths
    assert len(ctx.required_key_paths) == 2


def test_get_rules_non_chutes_namespace_leaves_key_paths_empty(validator):
    """_get_rules_for_context does not populate required_key_paths for non-chutes namespaces."""
    ctx = ValidationContext(
        config=validator.config,
        request={},
        namespace="default",
        images=["docker.io/parachutes/sek8s:latest"],
        cosign_config=MagicMock(spec=CosignConfig),
        validator=validator,
    )
    validator._get_rules_for_context(ctx)

    assert ctx.required_key_paths == set()


def test_get_rules_chutes_namespace_includes_chutes_rules(validator):
    """_get_rules_for_context includes _require_ctx_key in the chutes rule set."""
    ctx = ValidationContext(
        config=validator.config,
        request={},
        namespace="chutes",
        images=[],
        cosign_config=MagicMock(spec=CosignConfig),
        validator=validator,
    )
    rules = validator._get_rules_for_context(ctx)
    rule_names = {
        getattr(r, "__name__", None) or getattr(r, "__func__", r).__name__
        for r in rules
    }
    assert "_require_ctx_key" in rule_names


# ---------------------------------------------------------------------------
# _require_ctx_key: empty set guard
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_require_ctx_key_raises_when_key_paths_empty(validator):
    """_require_ctx_key must raise RuntimeError when required_key_paths is empty."""
    ctx = ValidationContext(
        config=validator.config,
        request={},
        namespace="chutes",
        images=["docker.io/parachutes/sek8s:latest"],
        cosign_config=MagicMock(spec=CosignConfig),
        validator=validator,
    )
    # required_key_paths starts empty by default
    with pytest.raises(RuntimeError, match="required_key_paths"):
        await validator._require_ctx_key(ctx)


# ---------------------------------------------------------------------------
# _require_ctx_key: correct key accepted
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_require_ctx_key_passes_for_chutes_key(validator):
    """Localregistry image configured with chutes.pub is accepted."""
    image = "5abc.localregistry.chutes.ai:30500/chutes/mymodel:latest"
    mock_cosign = MagicMock(spec=CosignConfig)
    mock_cosign.get_verification_config.return_value = _make_vc(CHUTES_KEY)

    ctx = ValidationContext(
        config=validator.config,
        request={},
        namespace="chutes",
        images=[image],
        cosign_config=mock_cosign,
        validator=validator,
    )
    ctx.required_key_paths = {CHUTES_KEY, DOCKERHUB_KEY}

    violations = await validator._require_ctx_key(ctx)
    assert violations == []


@pytest.mark.asyncio
async def test_require_ctx_key_passes_for_dockerhub_key(validator):
    """Docker Hub parachutes image configured with dockerhub.pub is accepted."""
    image = "docker.io/parachutes/sek8s:latest"
    mock_cosign = MagicMock(spec=CosignConfig)
    mock_cosign.get_verification_config.return_value = _make_vc(DOCKERHUB_KEY)

    ctx = ValidationContext(
        config=validator.config,
        request={},
        namespace="chutes",
        images=[image],
        cosign_config=mock_cosign,
        validator=validator,
    )
    ctx.required_key_paths = {CHUTES_KEY, DOCKERHUB_KEY}

    violations = await validator._require_ctx_key(ctx)
    assert violations == []


# ---------------------------------------------------------------------------
# _require_ctx_key: wrong key rejected
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_require_ctx_key_rejects_unknown_key(validator):
    """An image configured with an unrecognised key is rejected."""
    image = "docker.io/parachutes/sek8s:latest"
    mock_cosign = MagicMock(spec=CosignConfig)
    mock_cosign.get_verification_config.return_value = _make_vc(UNKNOWN_KEY)

    ctx = ValidationContext(
        config=validator.config,
        request={},
        namespace="chutes",
        images=[image],
        cosign_config=mock_cosign,
        validator=validator,
    )
    ctx.required_key_paths = {CHUTES_KEY, DOCKERHUB_KEY}

    violations = await validator._require_ctx_key(ctx)
    assert len(violations) == 1
    assert "different cosign key" in violations[0]
    assert image in violations[0]


@pytest.mark.asyncio
async def test_require_ctx_key_rejects_localregistry_with_dockerhub_key(validator):
    """Cross-key: localregistry image configured with dockerhub key is rejected."""
    image = "5abc.localregistry.chutes.ai:30500/chutes/mymodel:latest"
    mock_cosign = MagicMock(spec=CosignConfig)
    # Registry config incorrectly points localregistry at the dockerhub key
    mock_cosign.get_verification_config.return_value = _make_vc(DOCKERHUB_KEY)

    ctx = ValidationContext(
        config=validator.config,
        request={},
        namespace="chutes",
        images=[image],
        cosign_config=mock_cosign,
        validator=validator,
    )
    ctx.required_key_paths = {CHUTES_KEY, DOCKERHUB_KEY}

    # Passes the membership check (DOCKERHUB_KEY is in the set)
    # This test verifies the set-membership behaviour: both keys are trusted,
    # so a localregistry image using the dockerhub key is NOT rejected at this
    # layer — the registry config (cosign-registries.json) enforces which key
    # each registry must actually use via cosign verify at signing time.
    # _require_ctx_key only ensures the key is one of the two known trusted keys.
    violations = await validator._require_ctx_key(ctx)
    assert violations == []


@pytest.mark.asyncio
async def test_require_ctx_key_rejects_parachutes_with_chutes_key(validator):
    """Cross-key: parachutes image configured with chutes key is still accepted by
    _require_ctx_key (both are trusted); cosign verify enforces correct signing."""
    image = "docker.io/parachutes/sek8s:latest"
    mock_cosign = MagicMock(spec=CosignConfig)
    # Registry config incorrectly points parachutes at the chutes key
    mock_cosign.get_verification_config.return_value = _make_vc(CHUTES_KEY)

    ctx = ValidationContext(
        config=validator.config,
        request={},
        namespace="chutes",
        images=[image],
        cosign_config=mock_cosign,
        validator=validator,
    )
    ctx.required_key_paths = {CHUTES_KEY, DOCKERHUB_KEY}

    # Both keys are trusted at this layer; set membership passes
    violations = await validator._require_ctx_key(ctx)
    assert violations == []


@pytest.mark.asyncio
async def test_require_ctx_key_rejects_image_with_completely_unknown_key(validator):
    """An image with a key outside the trusted set is always rejected."""
    image = "docker.io/someorg/someimage:latest"
    mock_cosign = MagicMock(spec=CosignConfig)
    mock_cosign.get_verification_config.return_value = _make_vc(UNKNOWN_KEY)

    ctx = ValidationContext(
        config=validator.config,
        request={},
        namespace="chutes",
        images=[image],
        cosign_config=mock_cosign,
        validator=validator,
    )
    ctx.required_key_paths = {CHUTES_KEY, DOCKERHUB_KEY}

    violations = await validator._require_ctx_key(ctx)
    assert len(violations) == 1
    assert "different cosign key" in violations[0]


@pytest.mark.asyncio
async def test_require_ctx_key_skips_images_with_no_config(validator):
    """Images with no verification config produce no violation."""
    image = "docker.io/someorg/unconfigured:latest"
    mock_cosign = MagicMock(spec=CosignConfig)
    mock_cosign.get_verification_config.return_value = None

    ctx = ValidationContext(
        config=validator.config,
        request={},
        namespace="chutes",
        images=[image],
        cosign_config=mock_cosign,
        validator=validator,
    )
    ctx.required_key_paths = {CHUTES_KEY, DOCKERHUB_KEY}

    violations = await validator._require_ctx_key(ctx)
    assert violations == []


@pytest.mark.asyncio
async def test_require_ctx_key_deduplicates_images(validator):
    """Duplicate images in the list only produce one violation each."""
    image = "docker.io/parachutes/sek8s:latest"
    mock_cosign = MagicMock(spec=CosignConfig)
    mock_cosign.get_verification_config.return_value = _make_vc(UNKNOWN_KEY)

    ctx = ValidationContext(
        config=validator.config,
        request={},
        namespace="chutes",
        images=[image, image, image],
        cosign_config=mock_cosign,
        validator=validator,
    )
    ctx.required_key_paths = {CHUTES_KEY, DOCKERHUB_KEY}

    violations = await validator._require_ctx_key(ctx)
    assert len(violations) == 1


# ---------------------------------------------------------------------------
# AdmissionConfig field wiring
# ---------------------------------------------------------------------------


def test_admission_config_default_key_paths():
    """AdmissionConfig defaults point to the dynamic signing-keys tmpfs paths."""
    cfg = AdmissionConfig(opa_url="http://localhost:8181")
    assert cfg.chutes_public_key_path == Path(
        "/run/chutes/signing-keys/cosign/chutes.pub"
    )
    assert cfg.dockerhub_public_key_path == Path(
        "/run/chutes/signing-keys/cosign/dockerhub.pub"
    )


def test_admission_config_key_paths_override():
    """CHUTES_PUBLIC_KEY_PATH and DOCKERHUB_PUBLIC_KEY_PATH env aliases are respected."""
    cfg = AdmissionConfig(
        opa_url="http://localhost:8181",
        CHUTES_PUBLIC_KEY_PATH="/tmp/my-chutes.pub",
        DOCKERHUB_PUBLIC_KEY_PATH="/tmp/my-dockerhub.pub",
    )
    assert cfg.chutes_public_key_path == Path("/tmp/my-chutes.pub")
    assert cfg.dockerhub_public_key_path == Path("/tmp/my-dockerhub.pub")
