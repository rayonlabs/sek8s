import asyncio
import hashlib
import subprocess  # nosec B404
import tempfile

from loguru import logger

from sek8s.exceptions import NonceError, TdxQuoteException
from sek8s.nonce import QUOTE_NONCE_HEX_LEN, validate_quote_nonce

QUOTE_GENERATOR_BINARY = "/usr/bin/tdx-quote-generator"
# The per-VM proxy cert setup_vm_tls generates in initramfs and the proxy serves; REPORTDATA must
# hash this exact cert so the validator's expected_cert_hash matches.
SERVER_CERT = "/run/chutes/proxy-tls/server.crt"
# 64-byte TDX REPORTDATA as hex: the 64-char nonce followed by the 64-char cert hash.
REPORT_DATA_HEX_LEN = QUOTE_NONCE_HEX_LEN * 2


class TdxQuoteProvider:
    """Async TDX quote provider with cert hash binding."""

    def _get_cert_hash(self) -> str:
        """
        Compute SHA-256 hash of the server certificate's public key.
        This binds the quote to the specific certificate being used.

        Returns:
            64-character hex string (SHA-256 hash)
        """
        try:
            # Extract public key from certificate
            pubkey_result = subprocess.run(  # nosec B603 B607
                ["openssl", "x509", "-in", SERVER_CERT, "-pubkey", "-noout"],
                capture_output=True,
                check=True,
                text=True,
            )

            # Convert public key to DER format and hash it
            der_result = subprocess.run(  # nosec B603 B607
                ["openssl", "pkey", "-pubin", "-outform", "der"],
                input=pubkey_result.stdout.encode("utf-8"),
                capture_output=True,
                check=True,
                text=False,
            )

            # Compute SHA-256 hash
            cert_hash = hashlib.sha256(der_result.stdout).hexdigest()

            logger.debug(f"Computed cert hash: {cert_hash}")
            return cert_hash

        except subprocess.CalledProcessError as e:
            logger.error(f"Failed to compute cert hash: {e}")
            raise TdxQuoteException(f"Failed to compute certificate hash: {e}")
        except Exception as e:
            logger.error(f"Unexpected error computing cert hash: {e}")
            raise TdxQuoteException(f"Unexpected error computing certificate hash: {e}")

    async def get_quote(self, nonce: str) -> bytes:
        """
        Generate a TDX quote with nonce and certificate hash in report data.

        Args:
            nonce: 64-character hex string (32 bytes)

        Returns:
            Raw quote bytes
        """
        try:
            # Get certificate hash
            cert_hash = self._get_cert_hash()

            # REPORTDATA is 64 bytes = 128 hex chars, split 64/64 as nonce ‖ cert_hash. Both halves
            # are fixed-width, so validate rather than truncate: an over-long nonce used to push
            # cert_hash out of the field, yielding a signed quote that bound no certificate.
            nonce = validate_quote_nonce(nonce)
            report_data = f"{nonce}{cert_hash}"

            if len(report_data) != REPORT_DATA_HEX_LEN:
                raise TdxQuoteException(
                    f"REPORTDATA must be exactly {REPORT_DATA_HEX_LEN} hex characters, "
                    f"got {len(report_data)} (cert_hash was {len(cert_hash)})"
                )

            with tempfile.NamedTemporaryFile(mode="rb", suffix=".bin") as fp:
                result = await asyncio.create_subprocess_exec(
                    *[
                        QUOTE_GENERATOR_BINARY,
                        "--report-data",
                        report_data,
                        "--hex",
                        "--output",
                        fp.name,
                    ],
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )

                await result.wait()

                if result.returncode == 0:
                    if result.stdout is None:
                        raise TdxQuoteException("No stdout from quote command")
                    result_output = await result.stdout.read()
                    logger.info(
                        f"Successfully generated quote with nonce and cert hash.\n{result_output.decode()}"
                    )

                    # Read the quote from the file
                    fp.seek(0)
                    quote_content = fp.read()

                    return quote_content
                else:
                    if result.stderr is None:
                        raise TdxQuoteException("No stderr from quote command")
                    result_output = await result.stderr.read()
                    logger.error(f"Failed to generate quote: {result_output.decode()}")
                    raise TdxQuoteException("Failed to generate quote.")
        except (NonceError, TdxQuoteException):
            raise
        except Exception as e:
            logger.error(f"Unexpected error generating TDX quote: {e}")
            raise TdxQuoteException(f"Unexpected error generating TDX quote: {e}")
