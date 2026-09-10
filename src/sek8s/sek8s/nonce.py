"""Quote nonce validation.

REPORTDATA is a fixed 64-byte field carrying nonce ‖ cert_hash as 128 hex chars. Both halves are
fixed-width by construction, so an over-long nonce would displace cert_hash out of the field
entirely and the hardware would sign a quote binding no certificate. The producer enforces the
width here rather than trusting every caller to pre-hash its input.

Distinct from the validator-signature nonce in sek8s_common.auth, which is a Unix timestamp.
"""

from sek8s.exceptions import NonceError

QUOTE_NONCE_HEX_LEN = (
    64  # 32 bytes; the other 64 hex chars of REPORTDATA are the cert hash
)


def validate_quote_nonce(nonce: str) -> str:
    """Return the normalized nonce, or raise NonceError.

    Mirrors chutes_nvevidence.util.validate_nonce so both evidence producers enforce one rule.
    """
    if not isinstance(nonce, str):
        raise NonceError(f"Nonce must be a string, got {type(nonce).__name__}")

    nonce = nonce.strip()

    if not nonce:
        raise NonceError("Nonce cannot be empty")

    if len(nonce) != QUOTE_NONCE_HEX_LEN:
        raise NonceError(
            f"Nonce must be exactly {QUOTE_NONCE_HEX_LEN} hex characters "
            f"(32 bytes), got {len(nonce)} characters"
        )

    try:
        bytes.fromhex(nonce)
    except ValueError as e:
        raise NonceError(
            f"Nonce must contain only hexadecimal characters (0-9, a-f): {e}"
        )

    return nonce.lower()
