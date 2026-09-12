# tests/unit/test_quote_nonce.py
"""
Unit tests for quote nonce validation.

REPORTDATA is a fixed 64-byte field holding nonce ‖ cert_hash as 128 hex chars. The producer used
to concatenate and truncate to [:128], so an over-long nonce displaced cert_hash out of the field
and the hardware signed a quote binding no certificate. These tests pin the invariant.
"""

from unittest.mock import Mock, patch

import pytest

from sek8s.exceptions import NonceError
from sek8s.nonce import QUOTE_NONCE_HEX_LEN, validate_quote_nonce
from sek8s.providers.tdx import TdxQuoteProvider

VALID_NONCE = "a" * QUOTE_NONCE_HEX_LEN
CERT_HASH = "b" * 64
# The attack: a 64-char nonce followed by the attacker's own cert hash. Under the old truncation
# this became the whole of REPORTDATA and the real cert hash was discarded.
DISPLACING_NONCE = VALID_NONCE + ("c" * 64)


def test_accepts_valid_nonce():
    assert validate_quote_nonce(VALID_NONCE) == VALID_NONCE


def test_normalizes_case_and_whitespace():
    assert (
        validate_quote_nonce(f"  {'A' * QUOTE_NONCE_HEX_LEN}  ")
        == "a" * QUOTE_NONCE_HEX_LEN
    )


@pytest.mark.parametrize(
    "nonce",
    [
        pytest.param(DISPLACING_NONCE, id="displaces-cert-hash"),
        pytest.param("a" * 63, id="too-short"),
        pytest.param("a" * 65, id="too-long"),
        pytest.param("z" * QUOTE_NONCE_HEX_LEN, id="non-hex"),
        pytest.param("", id="empty"),
        pytest.param("   ", id="whitespace-only"),
    ],
)
def test_rejects_invalid_nonce(nonce):
    with pytest.raises(NonceError):
        validate_quote_nonce(nonce)


def test_rejects_non_string():
    with pytest.raises(NonceError):
        validate_quote_nonce(None)


def test_error_does_not_echo_nonce():
    """The message must not reflect caller input back into logs or a response body."""
    marker = "d" * QUOTE_NONCE_HEX_LEN + "deadbeef"
    with pytest.raises(NonceError) as exc:
        validate_quote_nonce(marker)
    assert marker not in str(exc.value)


@pytest.mark.asyncio
async def test_get_quote_rejects_displacing_nonce_before_generating():
    """The quote generator must never be invoked with attacker-chosen REPORTDATA."""
    provider = TdxQuoteProvider()
    with patch.object(provider, "_get_cert_hash", return_value=CERT_HASH):
        with patch("sek8s.providers.tdx.asyncio.create_subprocess_exec") as spawn:
            with pytest.raises(NonceError):
                await provider.get_quote(DISPLACING_NONCE)
            spawn.assert_not_called()


@pytest.mark.asyncio
async def test_get_quote_builds_full_width_report_data():
    """A valid nonce yields exactly 128 hex chars, with the cert hash still in the second half."""
    provider = TdxQuoteProvider()
    proc = Mock()
    proc.returncode = 0
    proc.wait = Mock(return_value=None)

    async def _wait():
        return None

    proc.wait = _wait
    proc.stdout = None

    with patch.object(provider, "_get_cert_hash", return_value=CERT_HASH):
        with patch("sek8s.providers.tdx.asyncio.create_subprocess_exec") as spawn:
            spawn.return_value = proc
            with pytest.raises(Exception):
                # stdout is None so the provider raises after spawning; we only care about argv.
                await provider.get_quote(VALID_NONCE)

    argv = spawn.call_args[0]
    report_data = argv[argv.index("--report-data") + 1]
    assert len(report_data) == 128
    assert report_data[:64] == VALID_NONCE
    assert report_data[64:] == CERT_HASH
