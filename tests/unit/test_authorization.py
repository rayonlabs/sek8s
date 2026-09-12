"""Unit tests for the /authorize endpoint (authorization webhook)."""

import pytest
from httpx import ASGITransport, AsyncClient

from sek8s.services.admission_controller import AdmissionWebhookServer


def _subject_access_review(
    user: str,
    resource: str = "pods",
    subresource: str = "log",
    namespace: str = "chutes",
    name: str = "some-pod",
    verb: str = "get",
) -> dict:
    return {
        "apiVersion": "authorization.k8s.io/v1",
        "kind": "SubjectAccessReview",
        "spec": {
            "user": user,
            "groups": ["system:authenticated"],
            "resourceAttributes": {
                "namespace": namespace,
                "verb": verb,
                "resource": resource,
                "subresource": subresource,
                "name": name,
                "group": "",
                "version": "v1",
            },
        },
    }


@pytest.fixture
def authz_client(webhook_server: AdmissionWebhookServer):
    """AsyncClient wired to the admission controller app."""
    transport = ASGITransport(app=webhook_server.app)
    return AsyncClient(transport=transport, base_url="https://test")


# ---------------------------------------------------------------------------
# Allowed prefixes -> NoOpinion (RBAC decides)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_allow_agent_pod_logs(authz_client):
    review = _subject_access_review(user="miner", name="agent-6f886cb54d-vfxx5")
    resp = await authz_client.post("/authorize", json=review)
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"]["allowed"] is False
    assert body["status"].get("denied") is not True


@pytest.mark.asyncio
async def test_allow_registry_pod_logs(authz_client):
    review = _subject_access_review(user="miner", name="chutes-registry-d2tdb")
    resp = await authz_client.post("/authorize", json=review)
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"]["allowed"] is False
    assert body["status"].get("denied") is not True


@pytest.mark.asyncio
async def test_allow_failed_chute_cleanup_logs(authz_client):
    review = _subject_access_review(
        user="miner", name="failed-chute-cleanup-29578898-8hdnn"
    )
    resp = await authz_client.post("/authorize", json=review)
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"]["allowed"] is False
    assert body["status"].get("denied") is not True


# ---------------------------------------------------------------------------
# Denied: pod name not in allowlist
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_deny_chute_workload_logs(authz_client):
    review = _subject_access_review(user="miner", name="chute-workload-abc123")
    resp = await authz_client.post("/authorize", json=review)
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"]["denied"] is True
    assert body["status"]["allowed"] is False


@pytest.mark.asyncio
async def test_deny_prefix_without_trailing_hyphen(authz_client):
    """'chutes-registryevil-pod' must NOT match the 'chutes-registry-' prefix."""
    review = _subject_access_review(user="miner", name="chutes-registryevil-pod")
    resp = await authz_client.post("/authorize", json=review)
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"]["denied"] is True


@pytest.mark.asyncio
async def test_deny_empty_pod_name(authz_client):
    review = _subject_access_review(user="miner", name="")
    resp = await authz_client.post("/authorize", json=review)
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"]["denied"] is True


# ---------------------------------------------------------------------------
# NoOpinion: non-miner user
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_opinion_system_user(authz_client):
    review = _subject_access_review(user="system:k3s-supervisor", name="registry-d2tdb")
    resp = await authz_client.post("/authorize", json=review)
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"]["allowed"] is False
    assert body["status"].get("denied") is not True


# ---------------------------------------------------------------------------
# NoOpinion: not pods/log subresource
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_opinion_get_pods(authz_client):
    review = _subject_access_review(user="miner", subresource="", name="chute-pod-xyz")
    resp = await authz_client.post("/authorize", json=review)
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"].get("denied") is not True


@pytest.mark.asyncio
async def test_no_opinion_exec_subresource(authz_client):
    review = _subject_access_review(
        user="miner", subresource="exec", name="registry-d2tdb"
    )
    resp = await authz_client.post("/authorize", json=review)
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"].get("denied") is not True


# ---------------------------------------------------------------------------
# NoOpinion: namespace outside chutes
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_opinion_kube_system(authz_client):
    review = _subject_access_review(
        user="miner", namespace="kube-system", name="coredns-abc123"
    )
    resp = await authz_client.post("/authorize", json=review)
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"].get("denied") is not True


@pytest.mark.asyncio
async def test_no_opinion_gpu_operator(authz_client):
    review = _subject_access_review(
        user="miner", namespace="gpu-operator", name="gpu-operator-pod-xyz"
    )
    resp = await authz_client.post("/authorize", json=review)
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"].get("denied") is not True


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_opinion_non_resource_request(authz_client):
    """Non-resource requests (no resourceAttributes) should get NoOpinion."""
    review = {
        "apiVersion": "authorization.k8s.io/v1",
        "kind": "SubjectAccessReview",
        "spec": {
            "user": "miner",
            "nonResourceAttributes": {"path": "/healthz", "verb": "get"},
        },
    }
    resp = await authz_client.post("/authorize", json=review)
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"].get("denied") is not True


@pytest.mark.asyncio
async def test_malformed_json(authz_client):
    """Malformed input must fail closed (denied), not pass through."""
    resp = await authz_client.post(
        "/authorize", content=b"not-json", headers={"content-type": "application/json"}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"]["denied"] is True
    assert body["status"]["allowed"] is False


@pytest.mark.asyncio
async def test_empty_spec(authz_client):
    review = {"apiVersion": "authorization.k8s.io/v1", "kind": "SubjectAccessReview"}
    resp = await authz_client.post("/authorize", json=review)
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"].get("denied") is not True
