"""Tests for graceful VM shutdown (chutes_cvm.guest.shutdown).

Signs a purpose-based request with the miner hotkey from config and POSTs it to the guest
system-manager shutdown endpoint. urllib is mocked; the sr25519 signing is real (a dummy seed).
"""

import io
import urllib.error
from unittest.mock import patch

import pytest
import yaml
from chutes_cvm.guest.shutdown import ShutdownError, graceful_shutdown

_SEED = "0x" + "11" * 32  # 32-byte hex seed → deterministic sr25519 keypair


def _write_cfg(tmp_path, *, seed=_SEED, vm_ip="192.168.100.2") -> str:
    data = {"network": {"vm_ip": vm_ip}}
    if seed is not None:
        data["miner"] = {"seed": seed}
    p = tmp_path / "config.yaml"
    p.write_text(yaml.safe_dump(data))
    return str(p)


class _Resp:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return b""


def test_graceful_posts_signed_request(tmp_path):
    captured = {}

    def _urlopen(req, timeout=None):
        captured["url"] = req.full_url
        captured["method"] = req.get_method()
        captured["headers"] = {k.lower(): v for k, v in req.header_items()}
        return _Resp()

    with patch(
        "chutes_cvm.guest.shutdown.urllib.request.urlopen", side_effect=_urlopen
    ):
        ip = graceful_shutdown(_write_cfg(tmp_path, vm_ip="10.0.0.9"))

    assert ip == "10.0.0.9"
    assert captured["url"] == "http://10.0.0.9:8080/status/system/shutdown"
    assert captured["method"] == "POST"
    h = captured["headers"]
    assert h["x-chutes-hotkey"] and h["x-chutes-nonce"] and h["x-chutes-signature"]


def test_graceful_without_seed_raises(tmp_path):
    with pytest.raises(ShutdownError, match="miner.seed"):
        graceful_shutdown(_write_cfg(tmp_path, seed=None))


def test_graceful_http_error_raises(tmp_path):
    def _urlopen(req, timeout=None):
        raise urllib.error.HTTPError(
            req.full_url, 401, "Unauthorized", {}, io.BytesIO(b"go away")
        )

    with patch(
        "chutes_cvm.guest.shutdown.urllib.request.urlopen", side_effect=_urlopen
    ):
        with pytest.raises(ShutdownError, match="401"):
            graceful_shutdown(_write_cfg(tmp_path))


def test_graceful_unreachable_raises(tmp_path):
    def _urlopen(req, timeout=None):
        raise urllib.error.URLError("connection refused")

    with patch(
        "chutes_cvm.guest.shutdown.urllib.request.urlopen", side_effect=_urlopen
    ):
        with pytest.raises(ShutdownError, match="could not reach"):
            graceful_shutdown(_write_cfg(tmp_path))
