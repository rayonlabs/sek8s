"""Tests for image_set.version_and_rc — the (version, rc) the launch/verify preflight joins on."""

from unittest.mock import patch

import pytest
from chutes_cvm.guest import image_set


def test_version_and_rc_reads_manifest():
    with patch.object(
        image_set, "_load_manifest", return_value={"version": "1.4.0", "debug": True}
    ):
        assert image_set.version_and_rc("/base") == ("1.4.0", True)


def test_version_and_rc_defaults_rc_false_without_debug_flag():
    with patch.object(image_set, "_load_manifest", return_value={"version": "1.4.0"}):
        assert image_set.version_and_rc("/base") == ("1.4.0", False)


def test_version_and_rc_requires_a_version():
    with patch.object(image_set, "_load_manifest", return_value={"debug": False}):
        with pytest.raises(ValueError, match="no version"):
            image_set.version_and_rc("/base")
