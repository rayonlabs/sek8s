"""System manager service: composes system-status, cache, and images routers on one app."""

from contextlib import asynccontextmanager

from fastapi import FastAPI
from loguru import logger

from sek8s.config import SystemManagerConfig, image_config
from sek8s.server import WebServer
from sek8s.system_manager.cache.manager import CacheManager
from sek8s.system_manager.cache.router import router as cache_router
from sek8s.system_manager.images.manager import ImageManager
from sek8s.system_manager.images.router import router as images_router
from sek8s.system_manager.status.router import router as status_router


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup/shutdown lifecycle for the system manager."""
    cache_mgr = CacheManager()
    logger.info("Initializing cache manager...")
    try:
        await cache_mgr.initialize()
    except Exception as e:
        logger.error("Cache manager initialization failed (non-fatal): {}", e)
    app.state.cache_manager = cache_mgr

    image_mgr = ImageManager(
        allowed_registries=image_config.image_pull_allowed_registries,
        pull_timeout=image_config.image_pull_timeout_seconds,
        default_org=image_config.image_pull_default_org,
    )
    app.state.image_manager = image_mgr

    yield


class SystemManagerServer(WebServer):
    """Web server for the system manager (status + cache + images routes)."""

    def __init__(self, config: SystemManagerConfig):
        super().__init__(config, lifespan=lifespan)

    def _setup_routes(self) -> None:
        self.app.include_router(status_router, prefix="/status", tags=["status"])
        self.app.include_router(cache_router, prefix="/cache", tags=["cache"])
        self.app.include_router(images_router, prefix="/images", tags=["images"])


def create_app() -> FastAPI:
    """Create the manager FastAPI app (for testing or programmatic use)."""
    config = SystemManagerConfig()
    server = SystemManagerServer(config)
    return server.app


def run() -> None:
    """Run the system-manager server (status + cache routes)."""
    config = SystemManagerConfig()
    server = SystemManagerServer(config)
    server.run()


if __name__ == "__main__":
    run()
