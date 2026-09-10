# tests/unit/test_proxy_hotkey_pop_scope.py
"""
The miner-hotkey proof-of-possession must be attached only to the responses that consume it.

The PoP signs {ss58}:{nonce}:"tee" and nothing about the request, and the API resolves `purpose`
ahead of `payload_hash` — so it is a bearer credential valid on every purpose="tee" route for its
freshness window. It is opt-in per endpoint (attach_hotkey_headers) rather than applied in the shared
proxy path, so a new route cannot inherit it.
"""

import inspect
from unittest.mock import AsyncMock, MagicMock

import pytest
from attestation_proxy.service import BaseProxyServer, ExternalProxyServer
from fastapi import Response
from fastapi.params import Depends

# Routes deliberately served without an authorize() dependency. Anything here is reachable by
# anyone on the internet via NodePort 30443, so it must never carry the PoP.
UNAUTHENTICATED_EXTERNAL_ROUTES = {"/health", "/server/health"}

POP_HEADERS = ("X-Chutes-Hotkey", "X-Chutes-Nonce", "X-Chutes-Signature")
_TEST_SEED = "0x" + "ab" * 32


def _keypair():
    from substrateinterface import Keypair

    return Keypair.create_from_seed(_TEST_SEED)


@pytest.fixture
def server(monkeypatch):
    """ExternalProxyServer with a miner seed, and all proxy I/O stubbed out."""
    from cryptography.hazmat.primitives.asymmetric.rsa import generate_private_key

    srv = ExternalProxyServer.__new__(ExternalProxyServer)
    srv.shared = MagicMock()
    srv.shared.consecutive_socket_failures = 0
    srv.shared.http_client = AsyncMock()
    srv.shared.unix_client = AsyncMock()
    srv.server_name = "EXTERNAL"
    srv._private_key = generate_private_key(public_exponent=65537, key_size=2048)
    srv._miner_keypair = _keypair()
    srv.app = MagicMock()
    srv.extract_client_cert_info = MagicMock(return_value={})

    async def _fake(self, *args, **kwargs):
        return Response(content=b"{}", status_code=srv.upstream_status)

    srv.upstream_status = 200
    monkeypatch.setattr(BaseProxyServer, "proxy_request", _fake)
    return srv


def _request():
    request = MagicMock()
    request.method = "GET"
    request.body = AsyncMock(return_value=b"")
    request.query_params = {}
    request.headers = {}
    return request


def _pop_headers_on(response):
    present = {k.lower() for k in response.headers}
    return [h for h in POP_HEADERS if h.lower() in present]


@pytest.mark.asyncio
async def test_unauthenticated_health_does_not_carry_the_pop(server):
    """The regression: an anonymous caller must not receive a usable credential."""
    response = await server.proxy_to_host_service_health(_request())
    assert not _pop_headers_on(response)


@pytest.mark.asyncio
async def test_devices_does_not_carry_the_pop(server):
    """Nothing reads the PoP off /server/devices, so it must not be handed one."""
    response = await server.proxy_devices_authenticated(_request(), _auth=True)
    assert not _pop_headers_on(response)


@pytest.mark.asyncio
async def test_shared_proxy_path_never_attaches_the_pop(server):
    """proxy_request signs the body but must not mint a bearer credential."""
    response = await server.proxy_request(
        target_url="http://localhost", method="GET", path="/attest", headers={}
    )
    assert "X-Signature" in response.headers  # body signature is bound and stays
    assert not _pop_headers_on(response)


@pytest.mark.asyncio
async def test_attest_route_carries_the_pop(server):
    """verify_server reads the proof off GET /server/attest — it must still be there."""
    response = await server.proxy_to_host_service_authenticated(
        "attest", _request(), _auth=True
    )
    assert sorted(_pop_headers_on(response)) == sorted(POP_HEADERS)


@pytest.mark.asyncio
async def test_chute_service_route_carries_the_pop(server):
    """The rc gate reads the proof off the chute evidence response."""
    response = await server.proxy_to_service_authenticated(
        "chute-service-abc", "evidence", _request(), _auth=True
    )
    assert sorted(_pop_headers_on(response)) == sorted(POP_HEADERS)


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [403, 500])
async def test_failed_responses_do_not_carry_the_pop(server, status):
    """A credential must not ride out on an error the caller provoked."""
    server.upstream_status = status
    response = await server.proxy_to_host_service_authenticated(
        "attest", _request(), _auth=True
    )
    assert not _pop_headers_on(response)


@pytest.mark.asyncio
async def test_no_seed_means_no_pop(server):
    server._miner_keypair = None
    response = await server.proxy_to_host_service_authenticated(
        "attest", _request(), _auth=True
    )
    assert not _pop_headers_on(response)


def test_no_new_unauthenticated_external_routes(server):
    """Fail if a route is added without an authorize() dependency.

    Such a route is anonymously reachable; confirm it does not call attach_hotkey_headers before
    adding it here.
    """
    server._setup_routes()

    unauthenticated = set()
    for call in server.app.add_api_route.call_args_list:
        path, handler = call.args[0], call.args[1]
        if not any(
            isinstance(p.default, Depends)
            for p in inspect.signature(handler).parameters.values()
        ):
            unauthenticated.add(path)

    assert unauthenticated == UNAUTHENTICATED_EXTERNAL_ROUTES, (
        "External proxy unauthenticated routes changed. Anything here is reachable anonymously "
        "over NodePort 30443; confirm it does not attach the hotkey PoP, then update "
        "UNAUTHENTICATED_EXTERNAL_ROUTES."
    )


def test_decorator_survives_fastapi_dependency_injection(server):
    """The stamp must happen on a real request, not just a direct Python call.

    A Depends() cannot do this job -- FastAPI merges an injected Response's headers only when it
    serializes the return value, and these handlers return a Response directly -- so this asserts
    the decorator does work end to end through the DI machinery.
    """
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    handler = server.proxy_to_host_service_authenticated
    auth_dep = inspect.signature(handler).parameters["_auth"].default.dependency

    app = FastAPI()
    app.add_api_route("/server/{path:path}", handler, methods=["GET"])
    app.dependency_overrides[auth_dep] = lambda: True

    response = TestClient(app).get("/server/attest")

    present = {k.lower() for k in response.headers}
    assert present >= {h.lower() for h in POP_HEADERS}


def test_auth_is_still_enforced_on_a_decorated_route(server):
    """Wrapping the handler must not bypass its authorize() dependency."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    app = FastAPI()
    app.add_api_route(
        "/server/{path:path}",
        server.proxy_to_host_service_authenticated,
        methods=["GET"],
    )

    response = TestClient(app, raise_server_exceptions=False).get("/server/attest")

    assert (
        response.status_code >= 400
    ), "decorated route answered an unauthenticated caller"
    present = {k.lower() for k in response.headers}
    assert not present & {h.lower() for h in POP_HEADERS}


# ── internal port must not expose the intra-namespace relay ──────────────────


def _registered_paths(server_cls) -> set[str]:
    """Collect the paths a server class registers, without standing up a real app."""
    srv = server_cls.__new__(server_cls)
    srv.app = MagicMock()
    paths: set[str] = set()
    srv.app.add_api_route.side_effect = lambda path, *a, **k: paths.add(path)
    srv._setup_routes()
    return paths


def test_internal_port_does_not_expose_service_relay():
    """`/service/{name}` on the unauthenticated port is a chute-to-chute relay.

    It dials another pod's :8002 in the workload namespace, and the netpolicies permit
    exactly that hop, so serving it without auth turns the proxy into the relay the
    chute egress policy exists to prevent. The chute only needs the attestation service.
    """
    from attestation_proxy.service import InternalProxyServer

    paths = _registered_paths(InternalProxyServer)
    assert not any(
        p.startswith("/service/") for p in paths
    ), f"internal port must not route /service/: {sorted(paths)}"
    # The attestation path a chute legitimately needs is still there.
    assert "/server/{path:path}" in paths


def test_external_port_still_serves_the_attestation_flow():
    """The authenticated copy backs client -> validator -> proxy -> chute; keep it."""
    paths = _registered_paths(ExternalProxyServer)
    assert "/service/{service_name}/{path:path}" in paths
