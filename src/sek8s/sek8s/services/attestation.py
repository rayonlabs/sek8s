import base64
import logging
from typing import Optional

from fastapi import HTTPException, Query, status
from loguru import logger

from sek8s.config import AttestationServiceConfig
from sek8s.exceptions import AttestationException, NonceError, NvmlException
from sek8s.models import DeviceInfo
from sek8s.providers.gpu import GpuDeviceProvider
from sek8s.providers.nvtrust import NvEvidenceProvider
from sek8s.providers.tdx import TdxQuoteProvider
from sek8s.responses import AttestationResponse
from sek8s.server import WebServer


def _normalize_gpu_ids(gpu_ids: Optional[list[str]]) -> Optional[list[str]]:
    """Expand comma-separated values so both repeated and CSV params work."""
    if not gpu_ids:
        return None

    normalized: list[str] = []
    for raw_value in gpu_ids:
        if not raw_value:
            continue
        normalized.extend(
            [value.strip() for value in raw_value.split(",") if value and value.strip()]
        )

    return normalized or None


class AttestationServer(WebServer):
    """Async web server for admission webhook."""

    config: AttestationServiceConfig

    def __init__(self, config: AttestationServiceConfig):
        super().__init__(config)
        self.config = config

    def _setup_routes(self):
        """Setup web routes."""
        self.app.add_api_route("/health", self.ping, methods=["GET"])
        self.app.add_api_route("/attest", self.attest, methods=["GET"])
        self.app.add_api_route("/devices", self.get_device_info, methods=["GET"])
        self.app.add_api_route("/tdx/quote", self.get_quote, methods=["GET"])
        self.app.add_api_route(
            "/nvtrust/evidence", self.get_nvtrust_evidence, methods=["GET"]
        )

    async def ping(self):
        return "pong"

    async def attest(
        self,
        nonce: str = Query(..., description="Nonce to include in the quote"),
        gpu_ids: Optional[list[str]] = Query(
            None,
            description="List of GPU IDs to use.  If not provided gets evidence for all devices.",
        ),
    ):
        try:
            gpu_ids = _normalize_gpu_ids(gpu_ids)
            tdx_provider = TdxQuoteProvider()
            with NvEvidenceProvider() as nvtrust_provider:
                quote_content = await tdx_provider.get_quote(nonce)
                nvtrust_evidence = await nvtrust_provider.get_evidence(
                    self.config.hostname, nonce, gpu_ids
                )

            return AttestationResponse(
                tdx_quote=base64.b64encode(quote_content).decode("utf-8"),
                nvtrust_evidence=nvtrust_evidence,
            )

        except NonceError as e:
            logger.warning(f"Rejected attestation request with invalid nonce: {e}")
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
        except AttestationException as e:
            logger.error(f"Error generating attestation evidence: {e}")
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e)
            )
        except Exception as e:
            logger.error(
                f"Unexpected exception encountered generating attestaion data: {e}"
            )
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Unexpected exception encountered generating attestaion data.",
            )

    async def get_device_info(
        self,
        gpu_ids: Optional[list[str]] = Query(
            None,
            description="List of GPU IDs to use.  If not provided gets all devices.",
        ),
    ) -> list[DeviceInfo]:
        try:
            gpu_ids = _normalize_gpu_ids(gpu_ids)
            gpu_provider = GpuDeviceProvider()
            device_info = gpu_provider.get_device_info(gpu_ids)

            return device_info
        except NvmlException as e:
            logger.error(f"Exception getting device info: {e}")
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to get device info.",
            )
        except Exception as e:
            logger.error(f"Unexpected exception encountered getting device info: {e}")
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Unexpected exception encountered getting device info.",
            )

    async def get_quote(
        self, nonce: str = Query(..., description="Nonce to include in the quote")
    ):
        try:
            provider = TdxQuoteProvider()
            quote_content = await provider.get_quote(nonce)

            return base64.b64encode(quote_content).decode("utf-8")
        except HTTPException:
            raise
        except NonceError as e:
            logger.warning(f"Rejected quote request with invalid nonce: {e}")
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
        except Exception as e:
            logger.error(f"Unexpected error generating TDX quote:{e}")
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Unexpected error generating TDX quote.",
            )

    async def get_nvtrust_evidence(
        self,
        name: str = Query(
            None, description="Name of the node to include in the evidence"
        ),
        nonce: str = Query(None, description="Nonce to include in the evidence"),
        gpu_ids: Optional[list[str]] = Query(
            None,
            description="List of GPU IDs to use.  If not provided gets evidence for all devices.",
        ),
    ):
        try:
            gpu_ids = _normalize_gpu_ids(gpu_ids)
            with NvEvidenceProvider() as provider:
                evidence = await provider.get_evidence(name, nonce, gpu_ids)

            return evidence
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Unexpected error gathering GPU evidence:{e}")
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Unexpected error gathering GPU evidence.",
            )


def run():
    """Main entry point."""
    try:
        # Load configuration using Pydantic
        config = AttestationServiceConfig()

        # Setup logging level based on config
        if config.debug:
            logging.getLogger().setLevel(logging.DEBUG)
            logger.debug("Debug mode enabled")
            logger.debug("Configuration: %s", config.export_json())

        # Validate required TLS configuration
        if not config.tls_cert_path or not config.tls_key_path:
            logger.warning("TLS certificates not configured, running in insecure mode")

        # Create and run server
        server = AttestationServer(config)
        server.run()

    except Exception as e:
        logger.exception("Failed to start Attestation service: %s", e)
        raise


if __name__ == "__main__":
    run()
