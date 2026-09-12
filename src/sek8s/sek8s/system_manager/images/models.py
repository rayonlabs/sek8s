"""Images submodule: request models and internal data types."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass
class ImageEntry:
    """Single image entry from containerd (parsed from k3s ctr images list)."""

    ref: str
    digest: Optional[str]
    size_bytes: Optional[int]
