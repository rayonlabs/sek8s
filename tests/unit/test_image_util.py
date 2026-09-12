"""Tests for system_manager.images.util functions."""

import pytest
from fastapi import HTTPException

from sek8s.system_manager.images.util import (
    is_registry_allowed,
    resolve_to_full_ref,
    validate_image_ref,
)

REGISTRY = "registry.chutes.ai"
ALLOWED = [REGISTRY]


# ── resolve_to_full_ref ────────────────────────────────────────────────────────


def test_resolve_full_ref_returned_unchanged():
    """A fully-qualified ref is returned as-is."""
    ref = f"{REGISTRY}/chutes/myrepo:latest"
    assert resolve_to_full_ref(ref, ALLOWED) == ref


def test_resolve_short_repo_tag():
    """repo:tag expands to registry/default_org/repo:tag."""
    assert resolve_to_full_ref("myrepo:v1", ALLOWED) == f"{REGISTRY}/chutes/myrepo:v1"


def test_resolve_short_repo_tag_custom_org():
    assert (
        resolve_to_full_ref("myrepo:v1", ALLOWED, default_org="parachutes")
        == f"{REGISTRY}/parachutes/myrepo:v1"
    )


def test_resolve_org_repo_tag():
    """org/repo:tag expands to registry/org/repo:tag."""
    assert (
        resolve_to_full_ref("myorg/myrepo:latest", ALLOWED)
        == f"{REGISTRY}/myorg/myrepo:latest"
    )


def test_resolve_strips_whitespace():
    assert (
        resolve_to_full_ref("  myrepo:v1  ", ALLOWED) == f"{REGISTRY}/chutes/myrepo:v1"
    )


def test_resolve_empty_raises_400():
    with pytest.raises(HTTPException) as exc_info:
        resolve_to_full_ref("", ALLOWED)
    assert exc_info.value.status_code == 400


def test_resolve_no_registry_hostname_in_allowed_raises_500():
    """If no registry.chutes.ai hostname is in allowed_registries, short-form fails."""
    with pytest.raises(HTTPException) as exc_info:
        resolve_to_full_ref("myrepo:v1", ["docker.io"])
    assert exc_info.value.status_code == 500
    assert "registry.chutes.ai" in exc_info.value.detail


def test_resolve_empty_allowed_list_raises_500():
    with pytest.raises(HTTPException) as exc_info:
        resolve_to_full_ref("myrepo:v1", [])
    assert exc_info.value.status_code == 500


# ── is_registry_allowed ────────────────────────────────────────────────────────


def test_is_registry_allowed_exact_match():
    assert is_registry_allowed(REGISTRY, ALLOWED) is True


def test_is_registry_allowed_case_insensitive():
    assert is_registry_allowed(REGISTRY.upper(), ALLOWED) is True


def test_is_registry_allowed_not_in_list():
    assert is_registry_allowed("docker.io", ALLOWED) is False


def test_is_registry_allowed_localhost_not_in_restricted_list():
    """localhost is not in the allowlist — image pull via localhost is blocked."""
    assert is_registry_allowed("localhost:30500", ALLOWED) is False


def test_is_registry_allowed_partial_match_not_sufficient():
    """Partial substring is not a match."""
    assert (
        is_registry_allowed("registry.chutes.ai.evil.com", ALLOWED) is False
    )  # superstring, not an exact match


# ── validate_image_ref ─────────────────────────────────────────────────────────


def test_validate_image_ref_valid():
    validate_image_ref(f"{REGISTRY}/chutes/myrepo:latest")


def test_validate_image_ref_empty_raises_400():
    with pytest.raises(HTTPException) as exc_info:
        validate_image_ref("")
    assert exc_info.value.status_code == 400


def test_validate_image_ref_with_newline_raises_400():
    with pytest.raises(HTTPException) as exc_info:
        validate_image_ref("myrepo:latest\nmalicious")
    assert exc_info.value.status_code == 400


def test_validate_image_ref_with_double_dash_raises_400():
    with pytest.raises(HTTPException) as exc_info:
        validate_image_ref("myrepo:latest--extra")
    assert exc_info.value.status_code == 400


def test_validate_image_ref_with_space_raises_400():
    with pytest.raises(HTTPException) as exc_info:
        validate_image_ref("my repo:latest")
    assert exc_info.value.status_code == 400


def test_validate_image_ref_too_long_raises_400():
    with pytest.raises(HTTPException) as exc_info:
        validate_image_ref("a" * 2049)
    assert exc_info.value.status_code == 400
