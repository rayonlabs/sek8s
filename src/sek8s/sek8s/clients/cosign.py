"""CosignClient: single source of truth for cosign verification.

Used by CosignValidator (admission control) and ImageManager (pull-time verify).
Keeps verification logic consistent across cosign version changes.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from pathlib import Path
from typing import Optional

from sek8s_common.constants import MTLS_CLIENT_CERT, MTLS_CLIENT_KEY

from sek8s.config import CosignVerificationConfig

logger = logging.getLogger(__name__)

# Avoid cosign writing to $HOME/.sigstore (rekor/TUF cache); system-manager runs
# as unprivileged user with no writable home. In-memory cache works for both
# admission controller and image pull verification.
# DOCKER_CONFIG is set by systemd (shared drop-in); inherit from os.environ — do not override here.
_COSIGN_ENV = {**os.environ, "SIGSTORE_NO_CACHE": "1"}

# The VM's mTLS client identity (shared across all CVM->Chutes mTLS), minted at boot.
# cosign/go-containerregistry ignores /etc/docker/certs.d, so the cert must be passed
# explicitly; it is presented only when registry.chutes.ai requests it, inert otherwise.
# Absent on build/test hosts, where the flags are omitted.
_MTLS_CLIENT_CERT = Path(os.environ.get("SEK8S_MTLS_CLIENT_CERT", MTLS_CLIENT_CERT))
_MTLS_CLIENT_KEY = Path(os.environ.get("SEK8S_MTLS_CLIENT_KEY", MTLS_CLIENT_KEY))


def _registry_mtls_args() -> list[str]:
    """`--registry-client-cert/key` flags for registry.chutes.ai mTLS, when the leaf exists."""
    if _MTLS_CLIENT_CERT.exists() and _MTLS_CLIENT_KEY.exists():
        return [
            "--registry-client-cert",
            str(_MTLS_CLIENT_CERT),
            "--registry-client-key",
            str(_MTLS_CLIENT_KEY),
        ]
    return []


class CosignRateLimitError(Exception):
    """Raised when upstream registry signals rate limiting."""


class CosignVerificationUnavailableError(Exception):
    """Raised when cosign cannot verify due to network/infra failure."""


def _extract_verified_digest(stdout: str) -> Optional[str]:
    """Extract the image digest from cosign's JSON verification output.

    Cosign outputs a JSON array on success. Each entry contains
    ``critical.image.docker-manifest-digest`` with the verified digest.
    """
    try:
        result = json.loads(stdout)
        if not isinstance(result, list) or not result:
            return None
        critical = result[0].get("critical", {})
        digest = critical.get("image", {}).get("docker-manifest-digest")
        if digest and isinstance(digest, str):
            return digest
    except (json.JSONDecodeError, AttributeError, IndexError, TypeError):
        pass
    return None


class CosignClient:
    """Client for verifying container image signatures via cosign subprocess."""

    def __init__(self) -> None:
        pass

    async def verify(
        self,
        image: str,
        config: CosignVerificationConfig,
        *,
        timeout: float = 60.0,
    ) -> tuple[bool, Optional[str]]:
        """Verify image signature.

        Returns ``(valid, verified_digest)`` where *verified_digest* is the
        ``sha256:…`` digest extracted from cosign's output on success, or
        ``None`` when verification fails or the digest cannot be parsed.
        """
        if config.verification_method == "key":
            return await self._verify_with_key(image, config, timeout=timeout)
        if config.verification_method == "keyless":
            return await self._verify_keyless(image, config, timeout=timeout)
        logger.error("Unknown verification method: %s", config.verification_method)
        return (False, None)

    async def _verify_with_key(
        self,
        image: str,
        config: CosignVerificationConfig,
        *,
        timeout: float = 60.0,
    ) -> tuple[bool, Optional[str]]:
        """Verify using public key."""
        if not config.public_key or not config.public_key.exists():
            logger.error("Cosign public key not found: %s", config.public_key)
            return (False, None)

        cmd = [
            "cosign",
            "verify",
            "--key",
            str(config.public_key),
        ]
        if config.allow_http:
            cmd.append("--allow-http-registry")
        if config.allow_insecure:
            cmd.append("--allow-insecure-registry")
        if config.rekor_url:
            cmd.extend(["--rekor-url", config.rekor_url])
        cmd.extend(_registry_mtls_args())
        cmd.append(image)

        success, stdout, stderr = await self._run_cosign(cmd, timeout=timeout)
        if not success:
            logger.warning("Cosign verify failed for %s: %s", image, stderr)
            return (False, None)
        return (True, _extract_verified_digest(stdout))

    async def _verify_keyless(
        self,
        image: str,
        config: CosignVerificationConfig,
        *,
        timeout: float = 60.0,
    ) -> tuple[bool, Optional[str]]:
        """Verify using keyless (OIDC)."""
        if not config.keyless_identity_regex or not config.keyless_issuer:
            logger.error("Keyless verification requires identity regex and issuer")
            return (False, None)

        cmd = [
            "cosign",
            "verify",
            "--certificate-identity-regexp",
            config.keyless_identity_regex,
            "--certificate-oidc-issuer",
            config.keyless_issuer,
            image,
        ]
        if config.rekor_url:
            cmd.extend(["--rekor-url", config.rekor_url])
        if config.fulcio_url:
            cmd.extend(["--fulcio-url", config.fulcio_url])
        cmd.extend(_registry_mtls_args())

        success, stdout, _stderr = await self._run_cosign(cmd, timeout=timeout)
        if success:
            try:
                result = json.loads(stdout)
                if isinstance(result, list) and len(result) > 0:
                    return (True, _extract_verified_digest(stdout))
            except json.JSONDecodeError:
                logger.error("Invalid JSON from cosign: %s", stdout)
                return (False, None)
        return (False, None)

    _RATE_LIMIT_PATTERNS = [
        re.compile(r"\brate\s*limit", re.IGNORECASE),
        re.compile(r"\b429\b"),
        re.compile(r"too many requests", re.IGNORECASE),
        re.compile(r"pull rate limit", re.IGNORECASE),
    ]
    _CONNECTION_FAILURE_INDICATORS = [
        "connection refused",
        "connection reset",
        "dial tcp",
        "i/o timeout",
        "temporary failure",
        "no such host",
        "connection timed out",
    ]

    async def _run_cosign(
        self,
        cmd: list[str],
        timeout: float = 60.0,
    ) -> tuple[bool, str, str]:
        """Run cosign command. Returns (success, stdout, stderr).
        Raises CosignRateLimitError or CosignVerificationUnavailableError when detected.
        """
        try:
            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=_COSIGN_ENV,
            )
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                process.communicate(), timeout=timeout
            )
            stdout = stdout_bytes.decode("utf-8", errors="replace")
            stderr = stderr_bytes.decode("utf-8", errors="replace")

            combined = f"{stdout}\n{stderr}"
            matched_pattern = next(
                (p.pattern for p in self._RATE_LIMIT_PATTERNS if p.search(combined)),
                None,
            )
            if matched_pattern:
                logger.error(
                    "Cosign rate limit detected for cmd=%s rc=%d pattern=%r\n"
                    "--- stdout ---\n%s\n--- stderr ---\n%s",
                    " ".join(cmd),
                    process.returncode,
                    matched_pattern,
                    stdout.strip() or "(empty)",
                    stderr.strip() or "(empty)",
                )
                raise CosignRateLimitError(
                    "Cosign verification rate limited by upstream registry"
                )
            if process.returncode != 0:
                combined_lower = combined.lower()
                if any(
                    ind in combined_lower for ind in self._CONNECTION_FAILURE_INDICATORS
                ):
                    raise CosignVerificationUnavailableError(
                        stderr or stdout or "Registry/network unavailable"
                    )
            return (process.returncode == 0, stdout, stderr)
        except (CosignRateLimitError, CosignVerificationUnavailableError):
            raise
        except asyncio.TimeoutError:
            logger.error("Cosign timeout after %.0fs", timeout)
            return (False, "", "timeout")
        except FileNotFoundError:
            logger.error("Cosign binary not found")
            return (False, "", "cosign not found")
        except Exception as e:
            logger.exception("Cosign error: %s", e)
            return (False, "", str(e))
