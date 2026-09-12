"""Tests for the changelog promotion helper (scripts/promote_changelogs.py)."""

import sys
from pathlib import Path

import pytest

# promote_changelogs.py lives under scripts/, not an installed package.
_SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from promote_changelogs import branch_to_fragment_name  # noqa: E402


@pytest.mark.parametrize(
    ("branch", "expected"),
    [
        # Any leading type/ prefix is stripped — not just a hardcoded set.
        ("feat/consolidate-topologies", "consolidate-topologies.md"),
        ("fix/host-tools", "host-tools.md"),
        ("feature/nvidia-590-drivers", "nvidia-590-drivers.md"),
        ("chore/cleanup", "cleanup.md"),
        ("refactor/reshape", "reshape.md"),
        # No prefix: used as-is.
        ("kernel-update", "kernel-update.md"),
        # Extra slashes flatten to a valid flat filename.
        ("feat/area/thing", "area-thing.md"),
    ],
)
def test_branch_to_fragment_name_strips_any_prefix(branch, expected):
    assert branch_to_fragment_name(branch) == expected
