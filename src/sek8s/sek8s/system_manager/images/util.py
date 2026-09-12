"""Images submodule: registry validation, ctr output parsing, image ref validation."""

from __future__ import annotations

import re
from typing import List

from fastapi import HTTPException

from .models import ImageEntry


def resolve_to_full_ref(
    image: str,
    allowed_registries: List[str],
    default_org: str = "chutes",
) -> str:
    """Resolve short form (repo:tag or org/repo:tag) to full registry ref.

    Since pulls are restricted to registry.chutes.ai, the registry can be inferred.
    - repo:tag       -> {registry}/{default_org}/repo:tag
    - org/repo:tag   -> {registry}/org/repo:tag
    - Full ref       -> returned as-is (validated against allowed list by caller)

    Chute workloads always reference images via registry.chutes.ai (the hostname
    baked into manifests at build time). Short-form refs must expand to that same
    hostname so the ref matches what k8s pods use.
    """
    image = image.strip()
    if not image:
        raise HTTPException(status_code=400, detail="image is required")

    # Full ref: has a registry host (contains a . or :) before the first /
    if "/" in image:
        first = image.split("/")[0]
        if "." in first or ":" in first:
            return image  # Already full ref

    # Short form: org/repo:tag or repo:tag
    # Resolve using the registry.chutes.ai hostname — chute workloads always
    # reference images with that hostname, so short-form refs must expand to it.
    registry = None
    for r in allowed_registries:
        if "registry.chutes.ai" in r.lower():
            registry = r
            break
    if registry is None:
        raise HTTPException(
            status_code=500,
            detail="allowed_registries must include the registry hostname (registry.chutes.ai); "
            "chute workloads resolve to that URL at build time",
        )
    if "/" in image:
        # org/repo:tag
        return f"{registry}/{image}"
    # repo:tag
    return f"{registry}/{default_org}/{image}"


def is_registry_allowed(registry: str, allowed: List[str]) -> bool:
    """Check if registry is in the allowed list (case-insensitive)."""
    reg_lower = registry.lower()
    for a in allowed:
        if a.lower() == reg_lower:
            return True
    return False


def parse_ctr_images_list(stdout: str) -> List[ImageEntry]:
    """Parse output of 'k3s ctr images list' into ImageEntry list.

    ctr uses space-aligned columns: REF, TYPE, DIGEST, SIZE, PLATFORMS, LABELS.
    SIZE may be like "72.6 MiB". We split by 2+ spaces to get columns.
    """
    entries: List[ImageEntry] = []
    for line in stdout.strip().splitlines():
        line = line.strip()
        if not line or line.startswith("REF"):
            continue
        parts = re.split(r"\s{2,}", line)
        ref = parts[0].strip() if parts else ""
        digest = None
        size_bytes = None
        for p in parts[1:]:
            if p.startswith("sha256:"):
                digest = p
                break
        for p in parts[1:]:
            # Match "123" or "72.6" (numeric size, possibly with MiB/GB suffix)
            m = re.match(r"^([\d.]+)\s*(MiB|GiB|KiB|MB|GB|KB)?$", p.strip())
            if m:
                try:
                    val = float(m.group(1))
                    unit = (m.group(2) or "").upper()
                    if "GIB" in unit or "GB" in unit:
                        val *= 1024**3
                    elif "MIB" in unit or "MB" in unit:
                        val *= 1024**2
                    elif "KIB" in unit or "KB" in unit:
                        val *= 1024
                    size_bytes = int(val)
                    break
                except (ValueError, TypeError):
                    pass
        if ref:
            entries.append(ImageEntry(ref=ref, digest=digest, size_bytes=size_bytes))
    return entries


# OCI image ref: alphanumeric, hyphen, underscore, dot, colon, slash, @ only.
# Reject: spaces, newlines, --, shell metachars, path traversal
_OCI_REF_PATTERN = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._/:@-]*$")


def validate_image_ref(image_ref: str) -> None:
    """Validate image reference format (OCI). Raises HTTPException if invalid."""
    if not image_ref or not image_ref.strip():
        raise HTTPException(status_code=400, detail="image_ref is required")
    ref = image_ref.strip()
    if len(ref) > 2048:
        raise HTTPException(status_code=400, detail="image_ref too long")
    if re.search(r"[\n\r\0]", ref):
        raise HTTPException(status_code=400, detail="Invalid characters in image_ref")
    if "--" in ref:
        raise HTTPException(status_code=400, detail="Invalid image_ref format")
    if not _OCI_REF_PATTERN.match(ref):
        raise HTTPException(
            status_code=400,
            detail="image_ref must use only alphanumeric, hyphen, underscore, dot, colon, slash, @",
        )
