"""Response signing utilities for the attestation proxy.

Signs response bodies with the host TLS private key (RSA-PKCS1v15-SHA256) so
that clients can verify the proxy holds the private key corresponding to the
TDX-attested public certificate.

Security invariant: no key material (bytes, PEM content, repr) is ever logged.
Only the key file path and metadata (type, size) are logged.
"""

import base64
import time
from pathlib import Path
from typing import Optional, Tuple

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPrivateKey
from loguru import logger
from substrateinterface import Keypair

# Purpose string the miner hotkey signs into the proof-of-possession ({ss58}:{nonce}:{purpose})
# attached to every proxied response. Reuses the platform's existing TEE request-auth purpose
# ("tee") -- the same one the validator's TEE endpoints already verify hotkey-mode proofs under --
# rather than inventing a parallel string. Must stay byte-identical to the value the validator
# re-verifies with. (Not RC-specific: the API happens to use it for the rc gate, but the proxy
# stamps it on all responses.)
HOTKEY_SIGNING_PURPOSE = "tee"


def load_private_key(path: Optional[Path]) -> Optional[RSAPrivateKey]:
    """Load an RSA private key from a PEM file.

    Returns the key on success, or None if the path is absent, the file does
    not exist, or the contents cannot be parsed as an RSA private key.  Never
    logs key material.
    """
    if path is None:
        logger.warning("TLS key path is not configured; response signing disabled")
        return None

    try:
        key_bytes = Path(path).read_bytes()
        key = serialization.load_pem_private_key(key_bytes, password=None)
    except FileNotFoundError:
        logger.warning(
            f"TLS private key not found at {path}; response signing disabled"
        )
        return None
    except Exception as exc:
        logger.warning(
            f"Failed to load TLS private key from {path}: {exc}; response signing disabled"
        )
        return None

    if not isinstance(key, RSAPrivateKey):
        logger.warning(
            f"Key at {path} is not an RSA private key (got {type(key).__name__}); "
            "response signing disabled"
        )
        return None

    key_size = key.key_size
    logger.info(f"Loaded {key_size}-bit RSA private key from {path}")
    return key


def sign_response_body(key: RSAPrivateKey, body: bytes) -> str:
    """Sign *body* with *key* using RSA-PKCS1v15-SHA256.

    Returns the signature as a base64-encoded string suitable for use in an
    HTTP header value.
    """
    signature = key.sign(body, padding.PKCS1v15(), hashes.SHA256())
    return base64.b64encode(signature).decode("ascii")


def load_miner_keypair(seed: Optional[str]) -> Optional[Keypair]:
    """Load the miner sr25519 keypair from its seed, or None when no seed is configured.

    Returning None (no seed) is the backwards-compatible path: old charts don't inject the
    seed, so the proxy simply doesn't sign. Never logs seed material -- only the derived ss58.
    """
    if not seed:
        logger.info("No miner seed configured; hotkey response signing disabled")
        return None
    try:
        keypair = Keypair.create_from_seed(seed)
    except Exception as exc:
        logger.warning(
            f"Failed to build miner keypair from seed: {exc}; hotkey signing disabled"
        )
        return None
    logger.info(f"Loaded miner hotkey {keypair.ss58_address} for response signing")
    return keypair


def sign_response(keypair: Keypair) -> Tuple[str, str, str]:
    """Return ``(ss58, nonce, signature_hex)`` -- an sr25519 proof-of-possession over
    ``{ss58}:{nonce}:{HOTKEY_SIGNING_PURPOSE}`` with a fresh timestamp nonce.

    Attached to every proxied response (see ``service.proxy_request``). This is a standalone
    hotkey liveness/PoP -- it does NOT sign the response body or bind to the request (that is
    ``sign_response_body``'s RSA signature). Mirrors the platform's standard request-auth signing
    message (``get_signing_message`` with a purpose) so the validator re-verifies it with the same
    primitive. The nonce is a unix timestamp; the validator's ``nonce_is_valid`` bounds replay to
    a short freshness window.
    """
    nonce = str(int(time.time()))
    message = f"{keypair.ss58_address}:{nonce}:{HOTKEY_SIGNING_PURPOSE}"
    signature = keypair.sign(message).hex()
    return keypair.ss58_address, nonce, signature
