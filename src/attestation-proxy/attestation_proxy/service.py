import asyncio
import logging
import os
import stat
from contextlib import asynccontextmanager
from functools import wraps
from typing import Dict, Optional
from urllib.parse import urljoin

import backoff
import httpx
from attestation_proxy.config import AttestationProxyConfig
from attestation_proxy.signing import (
    load_miner_keypair,
    load_private_key,
    sign_response,
    sign_response_body,
)
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPrivateKey
from fastapi import Depends, FastAPI, HTTPException, Request, Response
from loguru import logger
from sek8s_common.auth import authorize
from sek8s_common.server import WebServer

SERVICE_NAMESPACE = os.getenv("WORKLOAD_NAMESPACE", "chutes")
CLUSTER_DOMAIN = "svc.cluster.local"
SOCKET_PATH = "/var/run/attestation/attestation.sock"
MAX_CONSECUTIVE_FAILURES = 5

EXTERNAL_PORT = int(os.getenv("EXTERNAL_PORT", "8443"))
INTERNAL_PORT = int(os.getenv("INTERNAL_PORT", "8444"))
SERVICE_PORT = int(os.getenv("SERVICE_PORT", "8002"))


class SharedProxyResources:
    """Shared resources used by both internal and external proxy servers."""

    def __init__(self):
        self.unix_client: Optional[httpx.AsyncClient] = None
        self.http_client: Optional[httpx.AsyncClient] = None
        self.consecutive_socket_failures = 0
        self._initialized = False
        self._lock = asyncio.Lock()

    async def initialize(self):
        """Initialize shared HTTP clients (idempotent)."""
        async with self._lock:
            if self._initialized:
                logger.debug("Shared resources already initialized, skipping")
                return

            logger.info("Initializing shared proxy resources...")

            # Intra-cluster service-to-service communication over the pod network;
            # k8s services use certs issued by the cluster CA which is not in the
            # system trust store. NetworkPolicy restricts who can reach this client.
            self.http_client = httpx.AsyncClient(
                timeout=httpx.Timeout(30.0),
                verify=False,  # nosec B501
            )

            try:
                self.unix_client = httpx.AsyncClient(
                    transport=httpx.AsyncHTTPTransport(uds=SOCKET_PATH),
                    base_url="http://localhost",
                    timeout=httpx.Timeout(30.0),
                )
                logger.info("Unix socket client initialized successfully")
            except Exception as e:
                logger.warning(f"Failed to initialize Unix socket client: {e}")

            self._initialized = True
            logger.info("Shared proxy resources initialized")

    async def cleanup(self):
        """Cleanup shared HTTP clients"""
        async with self._lock:
            if not self._initialized:
                return

            if self.unix_client:
                await self.unix_client.aclose()
            if self.http_client:
                await self.http_client.aclose()

            self._initialized = False
            logger.info("Shared proxy resources cleaned up")

    def is_valid_socket(self) -> bool:
        """Check if socket path exists and is a valid socket file"""
        try:
            if not os.path.exists(SOCKET_PATH):
                return False
            stat_info = os.stat(SOCKET_PATH)
            return stat.S_ISSOCK(stat_info.st_mode)
        except OSError as e:
            logger.warning(f"Error checking socket {SOCKET_PATH}: {e}")
            return False


class BaseProxyServer(WebServer):
    """Base proxy server with shared functionality."""

    @staticmethod
    def _error_detail(generic: str, exc: BaseException, expose: bool) -> str:
        """The upstream error text, but only for a caller allowed to see it.

        An httpx error carries the address it was dialling, so this is gated per ROUTE
        rather than per port. Port is the wrong granularity: the external app also
        serves `/server/health` with no authentication at all, and `/server/devices`
        with `allow_miner=True` — the miner being the party the guest keeps tenants
        from. Only the two validator-only routes pass ``expose=True``, because the
        validator cannot log into the guest and would otherwise have to ask the miner
        to fetch the journal for it. Everyone else gets the fixed string; the detail is
        logged either way.
        """
        return f"{generic}: {exc}" if expose else generic

    def __init__(
        self,
        config: AttestationProxyConfig,
        shared_resources: SharedProxyResources,
        server_name: str,
    ):
        self.shared = shared_resources
        self.server_name = server_name

        @asynccontextmanager
        async def lifespan(app: FastAPI):
            logger.info(f"[{self.server_name}] Lifespan starting...")
            await self.shared.initialize()
            logger.info(f"[{self.server_name}] Lifespan startup complete")
            yield
            logger.info(f"[{self.server_name}] Lifespan shutdown...")

        super().__init__(config, lifespan=lifespan)

    def extract_client_cert_info(self, request: Request) -> Dict[str, str]:
        """Extract client certificate information from headers"""
        return {
            "X-Client-Cert": request.headers.get("X-Client-Cert", ""),
            "X-Client-Verify": request.headers.get("X-Client-Verify", ""),
            "X-Client-S-DN": request.headers.get("X-Client-S-DN", ""),
            "X-Client-I-DN": request.headers.get("X-Client-I-DN", ""),
            "X-Real-IP": request.headers.get("X-Real-IP", ""),
            "X-Forwarded-For": request.headers.get("X-Forwarded-For", ""),
            "X-Forwarded-Proto": request.headers.get("X-Forwarded-Proto", ""),
        }

    @backoff.on_exception(backoff.expo, httpx.ConnectError, max_tries=2, max_time=5)
    async def proxy_request(
        self,
        target_url: str,
        method: str,
        path: str,
        headers: Dict[str, str],
        body: bytes = b"",
        params: Optional[Dict[str, str]] = None,
        use_unix_socket: bool = False,
        expose_errors: bool = False,
    ) -> Response:
        """Proxy request with automatic retry on connection errors."""

        client = self.shared.unix_client if use_unix_socket else self.shared.http_client
        full_url = urljoin(target_url, path)

        filtered_headers = {
            k: v
            for k, v in headers.items()
            if k.lower()
            not in [
                "host",
                "connection",
                "upgrade",
                "proxy-authenticate",
                "proxy-authorization",
                "te",
                "trailers",
                "transfer-encoding",
            ]
        }

        if client is None:
            raise HTTPException(
                status_code=503, detail="Proxy HTTP client not initialized"
            )

        try:
            logger.info(f"Proxying {method} {full_url}")

            response = await client.request(
                method=method,
                url=full_url,
                headers=filtered_headers,
                content=body,
                params=params,
                follow_redirects=False,
            )

            if use_unix_socket:
                self.shared.consecutive_socket_failures = 0

            response_headers = {
                k: v
                for k, v in response.headers.items()
                if k.lower()
                not in [
                    "connection",
                    "upgrade",
                    "proxy-authenticate",
                    "proxy-authorization",
                    "te",
                    "trailers",
                    "transfer-encoding",
                    "server",
                ]
            }

            return Response(
                content=response.content,
                status_code=response.status_code,
                headers=response_headers,
                media_type=response.headers.get("content-type"),
            )

        except httpx.ConnectError as e:
            logger.error(f"Connection failed to {full_url}: {e}")
            if use_unix_socket:
                self.shared.consecutive_socket_failures += 1
                logger.warning(
                    f"Unix socket connection failed ({self.shared.consecutive_socket_failures} consecutive failures). "
                    f"Health check will trigger pod restart at {MAX_CONSECUTIVE_FAILURES} failures."
                )
            raise
        except httpx.RequestError as e:
            logger.error(f"Request failed to {full_url}: {e}")
            if use_unix_socket:
                self.shared.consecutive_socket_failures += 1
            raise HTTPException(
                status_code=502,
                detail=self._error_detail("Upstream unavailable", e, expose_errors),
            )
        except Exception as e:
            logger.error(f"Unexpected error proxying to {full_url}: {e}")
            if use_unix_socket:
                self.shared.consecutive_socket_failures += 1
            raise HTTPException(
                status_code=500,
                detail=self._error_detail("Internal proxy error", e, expose_errors),
            )

    async def health_check(self):
        """Health check endpoint"""
        socket_valid = self.shared.is_valid_socket()
        too_many_failures = (
            self.shared.consecutive_socket_failures >= MAX_CONSECUTIVE_FAILURES
        )

        if not socket_valid:
            logger.error(f"Health check failed: Unix socket invalid at {SOCKET_PATH}")
            return Response(
                content="unhealthy: unix socket unavailable",
                status_code=503,
                media_type="text/plain",
            )

        if too_many_failures:
            logger.error(
                f"Health check failed: {self.shared.consecutive_socket_failures} consecutive failures"
            )
            return Response(
                content=f"unhealthy: {self.shared.consecutive_socket_failures} consecutive socket failures",
                status_code=503,
                media_type="text/plain",
            )

        return {
            "status": "healthy",
            "service": "attestation-proxy",
            "socket_valid": socket_valid,
            "consecutive_failures": self.shared.consecutive_socket_failures,
        }

    async def not_found_handler(self, request: Request, exc):
        """Custom 404 handler"""
        # The path is the caller's own input, so echoing it reveals nothing -- but
        # reflecting request input into a response body is a shape worth not having.
        return Response(
            content="Proxy route not found",
            status_code=404,
            media_type="text/plain",
        )

    async def proxy_to_host_service(
        self, path: str, request: Request, *, expose_errors: bool = False
    ):
        """Proxy requests to host attestation service via Unix socket"""
        method = request.method
        body = await request.body()
        params = dict(request.query_params)
        headers = self.extract_client_cert_info(request)

        for key, value in request.headers.items():
            if key.lower() not in ["host", "content-length"]:
                headers[key] = value

        return await self.proxy_request(
            target_url="http://localhost",
            method=method,
            path=f"/{path}",
            headers=headers,
            body=body,
            params=params,
            use_unix_socket=True,
            expose_errors=expose_errors,
        )

    async def proxy_to_service(
        self,
        service_name: str,
        path: str,
        request: Request,
        *,
        expose_errors: bool = False,
    ):
        """Proxy requests to K8s workload services"""
        if not service_name.replace("-", "").replace("_", "").isalnum():
            raise HTTPException(status_code=400, detail="Invalid service name")

        method = request.method
        body = await request.body()
        params = dict(request.query_params)
        headers = self.extract_client_cert_info(request)

        for key, value in request.headers.items():
            if key.lower() not in ["host", "content-length"]:
                headers[key] = value

        service_url = (
            f"http://{service_name}.{SERVICE_NAMESPACE}.{CLUSTER_DOMAIN}:{SERVICE_PORT}"
        )

        return await self.proxy_request(
            target_url=service_url,
            method=method,
            path=f"/{path}",
            headers=headers,
            body=body,
            params=params,
            use_unix_socket=False,
            expose_errors=expose_errors,
        )


def attach_hotkey_headers(handler):
    """Attach the miner-hotkey proof-of-possession to this endpoint's response.

    Drop this on an endpoint whose response the validator reads the proof from, alongside its
    authorize() dependency. Opt-in per endpoint by design: the proof signs {ss58}:{nonce}:"tee"
    and nothing about the request, and the API resolves `purpose` ahead of `payload_hash`, so it
    is a bearer credential good on every purpose="tee" route for its freshness window. A route
    that does not need it must not carry it, and omitting it where it IS needed fails loudly
    (attestation is rejected) rather than leaking silently.

    A decorator and not a Depends() on purpose: FastAPI merges headers set on an injected Response
    only when it serializes the handler's return value, and these handlers return a Response
    object directly, which FastAPI passes through verbatim — so a dependency's headers are
    silently discarded. Wrapping the return value is what actually works.
    """

    @wraps(handler)
    async def wrapper(self, *args, **kwargs):
        response = await handler(self, *args, **kwargs)
        if self._miner_keypair is None or response.status_code >= 400:
            return response
        ss58, nonce, signature = sign_response(self._miner_keypair)
        response.headers["X-Chutes-Hotkey"] = ss58
        response.headers["X-Chutes-Nonce"] = nonce
        response.headers["X-Chutes-Signature"] = signature
        return response

    return wrapper


class ExternalProxyServer(BaseProxyServer):
    """External-facing proxy server with validator signature authentication."""

    def __init__(
        self, config: AttestationProxyConfig, shared_resources: SharedProxyResources
    ):
        super().__init__(config, shared_resources, "EXTERNAL")
        self._private_key: Optional[RSAPrivateKey] = load_private_key(
            config.tls_key_path
        )
        if self._private_key is None:
            raise RuntimeError(
                f"TLS private key could not be loaded from {config.tls_key_path}; "
                "cannot start external proxy without signing key"
            )
        # Optional: miner hotkey for the rc proof-of-possession. None when no seed is
        # configured (old charts) -> responses go out unsigned, unchanged behaviour.
        self._miner_keypair = load_miner_keypair(config.miner_seed)

    @backoff.on_exception(backoff.expo, httpx.ConnectError, max_tries=2, max_time=5)
    async def proxy_request(self, *args, **kwargs) -> Response:
        """Proxy request and attach an X-Signature header for key-possession proof.

        X-Signature covers the response body, so it is bound to what it signs and is safe on every
        response. The miner-hotkey proof-of-possession is NOT attached here -- see
        attach_hotkey_headers.
        """
        response = await super().proxy_request(*args, **kwargs)
        assert (
            self._private_key is not None
        )  # nosec B101 -- guaranteed by __init__ guard
        response.headers["X-Signature"] = sign_response_body(
            self._private_key, response.body
        )
        return response

    def _setup_routes(self):
        """Setup routes with validator authentication."""
        self.app.add_api_route("/health", self.health_check, methods=["GET"])

        self.app.add_api_route(
            "/server/health", self.proxy_to_host_service_health, methods=["GET"]
        )

        self.app.add_api_route(
            "/server/devices", self.proxy_devices_authenticated, methods=["GET"]
        )

        self.app.add_api_route(
            "/server/{path:path}",
            self.proxy_to_host_service_authenticated,
            methods=["GET", "POST", "PUT", "DELETE", "PATCH"],
        )

        self.app.add_api_route(
            "/service/{service_name}/{path:path}",
            self.proxy_to_service_authenticated,
            methods=["GET", "POST", "PUT", "DELETE", "PATCH"],
        )

        self.app.add_exception_handler(404, self.not_found_handler)

        logger.info(f"External server routes configured (port {EXTERNAL_PORT})")

    async def proxy_to_host_service_health(self, request: Request):
        """Proxy health check to host service without authentication"""
        return await self.proxy_to_host_service(path="health", request=request)

    async def proxy_devices_authenticated(
        self,
        request: Request,
        _auth: bool = Depends(
            authorize(allow_miner=True, allow_validator=True, purpose="attest")
        ),
    ):
        return await self.proxy_to_host_service(path="devices", request=request)

    @attach_hotkey_headers
    async def proxy_to_host_service_authenticated(
        self,
        path: str,
        request: Request,
        _auth: bool = Depends(authorize(allow_validator=True, purpose="attest")),
    ):
        """Proxy to host service with validator auth.

        Carries the hotkey PoP: this is the route GET /server/attest lands on, and verify_server
        reads the proof off that response.
        """
        return await self.proxy_to_host_service(path, request, expose_errors=True)

    @attach_hotkey_headers
    async def proxy_to_service_authenticated(
        self,
        service_name: str,
        path: str,
        request: Request,
        _auth: bool = Depends(authorize(allow_validator=True, purpose="attest")),
    ):
        """Proxy to K8s service with validator auth.

        Carries the hotkey PoP: chute evidence is fetched through here and the rc gate reads the
        proof off that response.
        """
        return await self.proxy_to_service(
            service_name, path, request, expose_errors=True
        )


class InternalProxyServer(BaseProxyServer):
    """Internal proxy server with no authentication (NetworkPolicy enforced).

    Chute pods reach this port for one thing: the attestation service, which this proxy
    forwards to over a unix socket. It deliberately does NOT expose `/service/{name}`.
    That route dials another pod's :8002 in the workload namespace, and the netpolicies
    permit exactly that hop (chute-proxy-access admits the proxy to chute pods on 8002)
    while the chute egress policy exists to stop chutes reaching each other directly —
    so serving it unauthenticated here would make this proxy the relay that policy is
    written to prevent. The authenticated copy on the external port serves the
    third-party attestation flow and is unaffected.
    """

    def __init__(
        self, config: AttestationProxyConfig, shared_resources: SharedProxyResources
    ):

        super().__init__(config, shared_resources, "INTERNAL")

    def _setup_routes(self):
        """Setup routes with no authentication."""

        self.app.add_api_route("/health", self.health_check, methods=["GET"])

        self.app.add_api_route(
            "/server/{path:path}",
            self.proxy_to_host_service,
            methods=["GET", "POST", "PUT", "DELETE", "PATCH"],
        )

        self.app.add_exception_handler(404, self.not_found_handler)

        logger.info(f"Internal server routes configured (port {INTERNAL_PORT})")


def run():
    """Main entry point."""
    try:
        os.environ["OPENBLAS_NUM_THREADS"] = "1"

        config = AttestationProxyConfig()

        if config.debug:
            logging.getLogger().setLevel(logging.DEBUG)
            logger.debug("Debug mode enabled")

        shared_resources = SharedProxyResources()

        external_config = AttestationProxyConfig()
        external_config.port = EXTERNAL_PORT
        external_server = ExternalProxyServer(external_config, shared_resources)

        internal_config = AttestationProxyConfig()
        internal_config.port = INTERNAL_PORT
        internal_server = InternalProxyServer(internal_config, shared_resources)

        logger.info(
            f"Starting attestation proxy with dual ports:\n"
            f"  - External port {EXTERNAL_PORT}: Validator signature required\n"
            f"  - Internal port {INTERNAL_PORT}: NetworkPolicy enforced, no auth"
        )

        async def run_both():
            try:
                logger.info("Launching both servers concurrently...")
                # Each server runs via the shared WebServer.serve(), so both
                # ports honour their own full config (TLS/mTLS/bind) — no
                # per-call-site uvicorn wiring that could drop a setting.
                await asyncio.gather(
                    external_server.serve(),
                    internal_server.serve(),
                )
            except Exception as e:
                logger.exception(f"Error running servers: {e}")
                raise
            finally:
                await shared_resources.cleanup()
                logger.info("Attestation proxy shutdown complete")

        asyncio.run(run_both())

    except Exception as e:
        logger.exception("Failed to start Attestation proxy service: %s", e)
        raise


if __name__ == "__main__":
    run()
