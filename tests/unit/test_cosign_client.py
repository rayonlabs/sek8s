"""Unit tests for CosignClient registry mTLS flag construction."""

import pytest

import sek8s.clients.cosign as cosign_mod
from sek8s.clients.cosign import CosignClient, _registry_mtls_args
from sek8s.config import CosignVerificationConfig


def _point_leaf_at(monkeypatch, cert, key):
    monkeypatch.setattr(cosign_mod, "_MTLS_CLIENT_CERT", cert)
    monkeypatch.setattr(cosign_mod, "_MTLS_CLIENT_KEY", key)


def test_registry_mtls_args_present_when_leaf_exists(monkeypatch, tmp_path):
    cert = tmp_path / "client.crt"
    key = tmp_path / "client.key"
    cert.write_text("cert")
    key.write_text("key")
    _point_leaf_at(monkeypatch, cert, key)

    assert _registry_mtls_args() == [
        "--registry-client-cert",
        str(cert),
        "--registry-client-key",
        str(key),
    ]


def test_registry_mtls_args_absent_when_leaf_missing(monkeypatch, tmp_path):
    # Only the cert exists; both are required, so no flags emitted.
    cert = tmp_path / "client.crt"
    cert.write_text("cert")
    _point_leaf_at(monkeypatch, cert, tmp_path / "missing.key")
    assert _registry_mtls_args() == []


async def _capture_cmd(monkeypatch, tmp_path, leaf_present):
    """Run _verify_with_key with _run_cosign stubbed, returning the built cmd."""
    key_file = tmp_path / "cosign.pub"
    key_file.write_text("pub")
    if leaf_present:
        cert = tmp_path / "client.crt"
        cert.write_text("c")
        key = tmp_path / "client.key"
        key.write_text("k")
        _point_leaf_at(monkeypatch, cert, key)
    else:
        _point_leaf_at(monkeypatch, tmp_path / "no.crt", tmp_path / "no.key")

    captured = {}

    async def fake_run(self, cmd, timeout=60.0):
        captured["cmd"] = cmd
        return (True, "[]", "")

    monkeypatch.setattr(CosignClient, "_run_cosign", fake_run)
    config = CosignVerificationConfig(verification_method="key", public_key=key_file)
    await CosignClient()._verify_with_key("registry.chutes.ai/chutes/x:1", config)
    return captured["cmd"]


@pytest.mark.asyncio
async def test_verify_with_key_appends_registry_mtls_flags(monkeypatch, tmp_path):
    cmd = await _capture_cmd(monkeypatch, tmp_path, leaf_present=True)
    assert "--registry-client-cert" in cmd
    assert "--registry-client-key" in cmd
    # Flags precede the positional image argument.
    assert cmd.index("--registry-client-cert") < cmd.index(
        "registry.chutes.ai/chutes/x:1"
    )


@pytest.mark.asyncio
async def test_verify_with_key_omits_flags_when_no_leaf(monkeypatch, tmp_path):
    cmd = await _capture_cmd(monkeypatch, tmp_path, leaf_present=False)
    assert "--registry-client-cert" not in cmd
