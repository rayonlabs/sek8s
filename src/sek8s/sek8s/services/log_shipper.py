"""chute-log-shipper service entrypoint.

Unlike the FastAPI services (system-manager, attestation, admission), the log
shipper is a headless asyncio daemon: it discovers chute pods via CRI, reads
their logs off disk, and ships them to the validator over mTLS.
"""

import asyncio

from loguru import logger
from sek8s_common.log_config import configure_logging

from sek8s.log_shipper.agent import LogShipperAgent
from sek8s.log_shipper.config import LogShipperConfig


async def _serve() -> None:
    config = LogShipperConfig()
    configure_logging(config.debug)
    logger.info(
        "Starting chute-log-shipper (validator={}, namespace={}, selector={})",
        config.validator_base_url,
        config.namespace,
        config.label_selector,
    )
    await LogShipperAgent(config).run()


def run() -> None:
    """Run the chute log shipper until interrupted."""
    try:
        asyncio.run(_serve())
    except KeyboardInterrupt:  # pragma: no cover - signal-driven shutdown
        logger.info("chute-log-shipper stopped")


if __name__ == "__main__":
    run()
