"""Images submodule: ImageManager for k3s/containerd image operations."""

from __future__ import annotations

import asyncio
from typing import List, Optional

from fastapi import HTTPException
from loguru import logger

from sek8s.image_utils import normalize_registry_hostname

from .models import ImageEntry
from .util import parse_ctr_images_list, resolve_to_full_ref, validate_image_ref

K3S_IMAGES_HELPER = "/usr/local/bin/k3s-images-helper"


class ImageManager:
    """Manages k3s/containerd images: list, delete, prune."""

    def __init__(
        self,
        *,
        allowed_registries: List[str],
        pull_timeout: float = 600.0,
        default_org: str = "chutes",
    ):
        self.allowed_registries = allowed_registries
        self.pull_timeout = pull_timeout
        self.default_org = default_org

    async def list_images(self) -> List[ImageEntry]:
        """List all images in containerd via k3s-images-helper."""
        code, stdout, stderr = await self._run("list", timeout=30.0)
        if code != 0:
            logger.error("k3s-images-helper list failed: {}", stderr)
            raise HTTPException(
                status_code=502,
                detail={"error": "ctr_list_failed", "stderr": stderr},
            )
        return parse_ctr_images_list(stdout)

    async def _run(
        self,
        subcommand: str,
        image_ref: Optional[str] = None,
        timeout: Optional[float] = None,
    ) -> tuple[int, str, str]:
        """Run k3s-images-helper (restricted wrapper). Returns (exit_code, stdout, stderr).
        Runs without sudo; system-manager must be in containerd group for socket access.
        """
        cmd = [K3S_IMAGES_HELPER, subcommand]
        if image_ref is not None:
            cmd.append(image_ref)
        to = timeout or self.pull_timeout
        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout_bytes, stderr_bytes = await asyncio.wait_for(
            process.communicate(), timeout=to
        )
        return (
            process.returncode if process.returncode is not None else -1,
            stdout_bytes.decode("utf-8", errors="replace"),
            stderr_bytes.decode("utf-8", errors="replace"),
        )

    async def delete_image(self, image: str, force: bool = False) -> None:
        """Remove image by reference or ID. Accepts short or full form."""
        image_ref = resolve_to_full_ref(
            image, self.allowed_registries, self.default_org
        )
        validate_image_ref(image_ref)
        rm_ref = normalize_registry_hostname(image_ref)
        code, stdout, stderr = await self._run(
            "rm",
            image_ref=rm_ref,
            timeout=30.0,
        )
        if code != 0:
            raise HTTPException(
                status_code=502,
                detail={"error": "ctr_rm_failed", "stderr": stderr or stdout},
            )

    async def prune(self) -> tuple[int, int]:
        """Prune unused images. Returns (removed_count, freed_bytes)."""
        code, stdout, stderr = await self._run("prune", timeout=120.0)
        if code != 0:
            raise HTTPException(
                status_code=502,
                detail={"error": "ctr_prune_failed", "stderr": stderr or stdout},
            )
        # ctr doesn't easily report freed bytes; we return 0 for now
        return (0, 0)
