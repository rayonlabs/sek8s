"""Unit tests for attestation_proxy.signing and X-Signature header injection."""

import base64
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from attestation_proxy.signing import (
    HOTKEY_SIGNING_PURPOSE,
    load_miner_keypair,
    load_private_key,
    sign_response,
    sign_response_body,
)
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.asymmetric.rsa import (
    RSAPrivateKey,
    generate_private_key,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_rsa_key(key_size: int = 2048) -> RSAPrivateKey:
    """Generate a fresh RSA private key for testing."""
    return generate_private_key(public_exponent=65537, key_size=key_size)


def _write_pem_key(path, key: RSAPrivateKey) -> None:
    pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.TraditionalOpenSSL,
        encryption_algorithm=serialization.NoEncryption(),
    )
    path.write_bytes(pem)


def _make_httpx_response(content: bytes = b"response body", status_code: int = 200):
    response = MagicMock()
    response.content = content
    response.status_code = status_code
    response.headers = {"content-type": "application/json"}
    return response


def _make_proxy_config():
    """Return a minimal AttestationProxyConfig-like mock."""
    config = MagicMock()
    config.tls_key_path = None
    return config


def _make_server(server_class, private_key=None, miner_keypair=None):
    """Construct a proxy server instance bypassing __init__ and inject attrs."""
    shared = MagicMock()
    shared.consecutive_socket_failures = 0
    shared.http_client = AsyncMock()
    shared.unix_client = None

    server = server_class.__new__(server_class)
    server.shared = shared
    server.server_name = server_class.__name__.upper()
    server._private_key = private_key
    server._miner_keypair = miner_keypair
    server.app = MagicMock()
    return server


# 32-byte hex seed for a deterministic sr25519 keypair in tests (never a real miner seed).
_TEST_SEED = "0x" + "ab" * 32


def _make_test_keypair():
    from substrateinterface import Keypair

    return Keypair.create_from_seed(_TEST_SEED)


# ---------------------------------------------------------------------------
# signing.sign_response_body
# ---------------------------------------------------------------------------


def test_sign_response_body_round_trip():
    key = _make_rsa_key()
    body = b"hello attestation world"

    encoded = sign_response_body(key, body)

    signature = base64.b64decode(encoded)
    key.public_key().verify(signature, body, padding.PKCS1v15(), hashes.SHA256())


def test_sign_response_body_returns_ascii_string():
    key = _make_rsa_key()
    result = sign_response_body(key, b"data")
    assert isinstance(result, str)
    result.encode("ascii")  # must not raise


def test_sign_response_body_empty_body():
    key = _make_rsa_key()
    encoded = sign_response_body(key, b"")
    signature = base64.b64decode(encoded)
    key.public_key().verify(signature, b"", padding.PKCS1v15(), hashes.SHA256())


# ---------------------------------------------------------------------------
# signing.load_private_key
# ---------------------------------------------------------------------------


def test_load_private_key_success(tmp_path):
    key = _make_rsa_key(key_size=2048)
    key_file = tmp_path / "server.key"
    _write_pem_key(key_file, key)

    loaded = load_private_key(key_file)

    assert loaded is not None
    assert isinstance(loaded, RSAPrivateKey)
    assert loaded.key_size == 2048


def test_load_private_key_missing_file(tmp_path):
    result = load_private_key(tmp_path / "nonexistent.key")
    assert result is None


def test_load_private_key_invalid_content(tmp_path):
    bad_file = tmp_path / "bad.key"
    bad_file.write_bytes(b"this is not a pem key")

    result = load_private_key(bad_file)
    assert result is None


def test_load_private_key_none_path():
    result = load_private_key(None)
    assert result is None


# ---------------------------------------------------------------------------
# X-Signature header injection via proxy_request
# ---------------------------------------------------------------------------


@pytest.fixture()
def rsa_key():
    return _make_rsa_key()


@pytest.mark.asyncio
async def test_proxy_request_adds_x_signature_header(rsa_key):
    from attestation_proxy.service import ExternalProxyServer

    body = b"attestation response content"
    server = _make_server(ExternalProxyServer, private_key=rsa_key)
    server.shared.http_client.request = AsyncMock(
        return_value=_make_httpx_response(content=body)
    )

    response = await server.proxy_request(
        target_url="http://fake-upstream",
        method="GET",
        path="/attest",
        headers={},
        body=b"",
    )

    assert "x-signature" in {k.lower() for k in response.headers.keys()}
    sig_header = next(
        v for k, v in response.headers.items() if k.lower() == "x-signature"
    )
    signature = base64.b64decode(sig_header)
    rsa_key.public_key().verify(signature, body, padding.PKCS1v15(), hashes.SHA256())


def test_external_proxy_raises_on_missing_key(monkeypatch, tmp_path):
    """ExternalProxyServer must refuse to start when the TLS key cannot be loaded."""
    from attestation_proxy.service import ExternalProxyServer, SharedProxyResources

    config = _make_proxy_config()
    config.tls_key_path = tmp_path / "nonexistent.key"

    # Suppress the lifespan so __init__ doesn't try to bind ports
    monkeypatch.setattr("attestation_proxy.service.asynccontextmanager", lambda f: f)

    with pytest.raises(RuntimeError, match="TLS private key could not be loaded"):
        ExternalProxyServer(config, SharedProxyResources())


@pytest.mark.asyncio
async def test_proxy_request_propagates_signing_error(rsa_key, monkeypatch):
    """A signing failure during a request must not be silently swallowed."""
    from attestation_proxy.service import ExternalProxyServer

    server = _make_server(ExternalProxyServer, private_key=rsa_key)
    server.shared.http_client.request = AsyncMock(
        return_value=_make_httpx_response(content=b"body")
    )

    monkeypatch.setattr(
        "attestation_proxy.service.sign_response_body",
        MagicMock(side_effect=RuntimeError("crypto failure")),
    )

    with pytest.raises(RuntimeError, match="crypto failure"):
        await server.proxy_request(
            target_url="http://fake-upstream",
            method="GET",
            path="/attest",
            headers={},
            body=b"",
        )


@pytest.mark.asyncio
async def test_internal_proxy_never_signs():
    from attestation_proxy.service import InternalProxyServer

    # Internal server has _private_key = None by default (set in BaseProxyServer.__init__),
    # so even if a key existed in shared resources it would not be used.
    server = _make_server(InternalProxyServer, private_key=None)
    server.shared.http_client.request = AsyncMock(
        return_value=_make_httpx_response(content=b"body")
    )

    response = await server.proxy_request(
        target_url="http://fake-upstream",
        method="GET",
        path="/attest",
        headers={},
        body=b"",
    )

    assert "x-signature" not in {k.lower() for k in response.headers.keys()}


# ---------------------------------------------------------------------------
# Server header stripping (regression: aiohttp 3.13.4 duplicate Server header)
# ---------------------------------------------------------------------------


def _make_httpx_response_with_server_header(
    content: bytes = b"response body",
    status_code: int = 200,
    server_header: str = "uvicorn",
):
    """Upstream response that includes a Server header, as FastAPI/Uvicorn backends do."""
    response = MagicMock()
    response.content = content
    response.status_code = status_code
    response.headers = {"content-type": "application/json", "server": server_header}
    return response


@pytest.mark.asyncio
async def test_proxy_request_strips_upstream_server_header_internal():
    """Upstream Server header must not be forwarded; Uvicorn adds its own.

    Forwarding it produces a duplicate Server header in the final HTTP response,
    which aiohttp 3.13.4+ rejects with 'Duplicate Server header found'.
    """
    from attestation_proxy.service import InternalProxyServer

    server = _make_server(InternalProxyServer, private_key=None)
    server.shared.http_client.request = AsyncMock(
        return_value=_make_httpx_response_with_server_header(content=b"body")
    )

    response = await server.proxy_request(
        target_url="http://fake-upstream",
        method="GET",
        path="/server/devices",
        headers={},
        body=b"",
    )

    assert "server" not in {k.lower() for k in response.headers.keys()}


# ---------------------------------------------------------------------------
# Miner-hotkey proof-of-possession signing
# ---------------------------------------------------------------------------


def test_load_miner_keypair_none_seed():
    assert load_miner_keypair(None) is None
    assert load_miner_keypair("") is None


def test_load_miner_keypair_invalid_seed():
    assert load_miner_keypair("not-a-valid-seed") is None


def test_load_miner_keypair_from_seed():
    keypair = load_miner_keypair(_TEST_SEED)
    assert keypair is not None
    assert keypair.ss58_address == _make_test_keypair().ss58_address


def test_sign_response_round_trip():
    from substrateinterface import Keypair

    keypair = _make_test_keypair()
    ss58, nonce, signature = sign_response(keypair)

    assert ss58 == keypair.ss58_address
    assert nonce.isdigit()
    message = f"{ss58}:{nonce}:{HOTKEY_SIGNING_PURPOSE}"
    # Verify exactly as the validator's rc gate does (hex signature over the string message).
    assert Keypair(ss58_address=ss58).verify(message, bytes.fromhex(signature))


@pytest.mark.asyncio
async def test_proxy_request_does_not_add_hotkey_headers(rsa_key):
    """The shared proxy path signs the body but must not mint the bearer PoP.

    The PoP is attached per endpoint via @attach_hotkey_headers, so a route that does not need
    it cannot inherit a credential. See test_proxy_hotkey_pop_scope.py.
    """
    from attestation_proxy.service import ExternalProxyServer

    keypair = _make_test_keypair()
    server = _make_server(
        ExternalProxyServer, private_key=rsa_key, miner_keypair=keypair
    )
    server.shared.http_client.request = AsyncMock(
        return_value=_make_httpx_response(content=b"body")
    )

    response = await server.proxy_request(
        target_url="http://fake-upstream",
        method="GET",
        path="/service/chute-service-x/verify",
        headers={},
        body=b"",
    )

    lower = {k.lower() for k in response.headers}
    assert "x-signature" in lower  # body signature is bound to what it signs and stays
    assert not lower & {"x-chutes-hotkey", "x-chutes-nonce", "x-chutes-signature"}


@pytest.mark.asyncio
async def test_attach_hotkey_headers_signs_the_expected_message(rsa_key):
    """The decorator produces a proof the validator can verify."""
    from attestation_proxy.service import ExternalProxyServer
    from substrateinterface import Keypair

    keypair = _make_test_keypair()
    server = _make_server(
        ExternalProxyServer, private_key=rsa_key, miner_keypair=keypair
    )
    server.shared.unix_client = AsyncMock()
    server.shared.unix_client.request = AsyncMock(
        return_value=_make_httpx_response(content=b"body")
    )
    server.extract_client_cert_info = MagicMock(return_value={})

    request = MagicMock()
    request.method = "GET"
    request.body = AsyncMock(return_value=b"")
    request.query_params = {}
    request.headers = {}

    response = await server.proxy_to_host_service_authenticated(
        "attest", request, _auth=True
    )

    lower = {k.lower(): v for k, v in response.headers.items()}
    assert lower["x-chutes-hotkey"] == keypair.ss58_address
    assert lower["x-chutes-nonce"].isdigit()
    message = (
        f"{keypair.ss58_address}:{lower['x-chutes-nonce']}:{HOTKEY_SIGNING_PURPOSE}"
    )
    assert Keypair(ss58_address=keypair.ss58_address).verify(
        message, bytes.fromhex(lower["x-chutes-signature"])
    )


def test_error_detail_is_exposed_only_on_validator_only_routes():
    """Upstream error text may go to the validator, never to a chute or the miner.

    An httpx error carries the address it was dialling. The gate is per ROUTE, not per
    port, because port is the wrong granularity: the external app also serves
    `/server/health` with no authentication at all, and `/server/devices` with
    `allow_miner=True` -- the miner being the party the guest keeps tenants from. So the
    invariant is that a handler opts into `expose_errors=True` only if its own
    `authorize(...)` admits the validator and nobody else.

    Checked on the AST rather than by grep so renaming a handler cannot quietly drop it
    out of the check.
    """
    import ast

    source = (
        Path(__file__).resolve().parents[2]
        / "src/attestation-proxy/attestation_proxy/service.py"
    ).read_text()
    tree = ast.parse(source)

    def authorize_kwargs(fn):
        """The authorize(...) kwargs on a handler's dependencies, if any."""
        for arg in list(fn.args.args) + list(fn.args.kwonlyargs):
            pass
        for default in fn.args.defaults + [d for d in fn.args.kw_defaults if d]:
            for node in ast.walk(default):
                if (
                    isinstance(node, ast.Call)
                    and getattr(node.func, "id", None) == "authorize"
                ):
                    return {
                        kw.arg: getattr(kw.value, "value", None) for kw in node.keywords
                    }
        return None

    exposing = set()
    handlers = {}
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        handlers[fn.name] = fn
        for node in ast.walk(fn):
            if isinstance(node, ast.Call) and any(
                kw.arg == "expose_errors"
                and isinstance(kw.value, ast.Constant)
                and kw.value.value is True
                for kw in node.keywords
            ):
                exposing.add(fn.name)

    assert exposing, "no handler exposes error detail; the gate has been removed"

    for name in exposing:
        auth = authorize_kwargs(handlers[name])
        assert auth is not None, f"{name} exposes error detail but has no authorize()"
        assert (
            auth.get("allow_validator") is True
        ), f"{name} does not admit the validator"
        assert not auth.get(
            "allow_miner"
        ), f"{name} exposes upstream error detail to the miner"


def test_error_detail_is_never_interpolated_directly_into_a_response():
    """Every error body must route through _error_detail, not format the exception.

    The gate only works if nothing bypasses it. A handler that builds its own
    f"...{e}" would return upstream text regardless of the route's authorisation, which
    is exactly the shape this finding was about.
    """
    source = (
        Path(__file__).resolve().parents[2]
        / "src/attestation-proxy/attestation_proxy/service.py"
    ).read_text()

    for line in source.splitlines():
        stripped = line.strip()
        if "detail=" not in stripped or "_error_detail" in stripped:
            continue
        assert (
            "{e}" not in stripped and "str(e)" not in stripped
        ), f"error detail bypasses the route gate: {stripped}"

    assert "request.url.path" not in source, "the 404 handler must not reflect the path"


def test_error_detail_helper_respects_the_flag():
    from attestation_proxy.service import BaseProxyServer

    upstream = RuntimeError("dialling http://chute-abc123.chutes.svc:8002/probe")
    assert (
        BaseProxyServer._error_detail("Upstream unavailable", upstream, False)
        == "Upstream unavailable"
    )
    assert "chute-abc123" in BaseProxyServer._error_detail(
        "Upstream unavailable", upstream, True
    )
