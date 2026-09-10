"""Unit tests for agent.py — keeping one shipper alive per chute pod."""

import asyncio

import pytest

from sek8s.log_shipper import agent as agent_module
from sek8s.log_shipper.agent import LogShipperAgent
from sek8s.log_shipper.config import LogShipperConfig
from sek8s.log_shipper.crictl import CrictlError
from sek8s.log_shipper.models import ChutePod


def pod(config_id: str) -> ChutePod:
    return ChutePod(
        config_id=config_id,
        name=f"pod-{config_id}",
        uid=f"uid-{config_id}",
        namespace="chutes",
        deployment_id=f"dep-{config_id}",
    )


class FakeShipper:
    """Stands in for PodLogShipper: records start/stop, fakes finishing."""

    instances: list["FakeShipper"] = []

    def __init__(self, pod, config, session, checkpoints):
        self.pod = pod
        self.started = False
        self.stopped = False
        self._running = False
        FakeShipper.instances.append(self)

    def start(self):
        self.started = True
        self._running = True

    @property
    def running(self):
        return self._running

    async def stop(self):
        self.stopped = True
        self._running = False

    def finish(self):
        """The validator ended capture: no longer running, but still tracked."""
        self._running = False


@pytest.fixture(autouse=True)
def fake_shipper(monkeypatch):
    FakeShipper.instances = []
    monkeypatch.setattr(agent_module, "PodLogShipper", FakeShipper)
    return FakeShipper


def make_agent(tmp_path, **overrides) -> LogShipperAgent:
    base = {
        "POD_LOG_ROOT": str(tmp_path),
        "CHECKPOINT_PATH": str(tmp_path / "ckpt.json"),
        "POLL_INTERVAL_SECONDS": 0.01,
    }
    base.update(overrides)
    return LogShipperAgent(LogShipperConfig(**base))


# ── sync ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_new_pod_gets_a_started_shipper(tmp_path):
    agent = make_agent(tmp_path)
    await agent._sync([pod("a")])
    assert set(agent._shippers) == {"a"}
    assert agent._shippers["a"].started


@pytest.mark.asyncio
async def test_a_pod_seen_again_is_not_restarted(tmp_path):
    agent = make_agent(tmp_path)
    await agent._sync([pod("a")])
    first = agent._shippers["a"]
    await agent._sync([pod("a")])
    assert agent._shippers["a"] is first
    assert len(FakeShipper.instances) == 1


@pytest.mark.asyncio
async def test_a_finished_shipper_is_not_restarted_while_the_pod_lives(tmp_path):
    """The reason a finished shipper stays in the map at all.

    The validator ended capture deliberately; the pod is still there. Restarting it
    would re-ship logs the validator has already said it does not want.
    """
    agent = make_agent(tmp_path)
    await agent._sync([pod("a")])
    agent._shippers["a"].finish()

    await agent._sync([pod("a")])
    assert len(FakeShipper.instances) == 1, "a finished capture must not be respawned"
    assert not agent._shippers["a"].running


@pytest.mark.asyncio
async def test_a_departed_pod_is_stopped_and_forgotten(tmp_path):
    agent = make_agent(tmp_path)
    await agent._sync([pod("a"), pod("b")])
    shipper_a = agent._shippers["a"]

    await agent._sync([pod("b")])
    assert shipper_a.stopped
    assert set(agent._shippers) == {"b"}


@pytest.mark.asyncio
async def test_a_pod_that_returns_after_leaving_starts_fresh(tmp_path):
    """Forgetting on departure is what lets a recreated pod capture again."""
    agent = make_agent(tmp_path)
    await agent._sync([pod("a")])
    await agent._sync([])
    await agent._sync([pod("a")])
    assert len(FakeShipper.instances) == 2
    assert agent._shippers["a"].started


# ── capacity ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_capacity_defers_the_excess_rather_than_dropping_it(tmp_path):
    agent = make_agent(tmp_path, MAX_CONCURRENT_PODS=2)
    await agent._sync([pod("a"), pod("b"), pod("c")])
    assert len(agent._shippers) == 2

    # The deferred pod is picked up once there is room.
    await agent._sync([pod("c")])
    assert set(agent._shippers) == {"c"}


@pytest.mark.asyncio
async def test_finished_shippers_do_not_consume_capacity(tmp_path):
    """They hold no task and no reader, so counting them would starve live pods."""
    agent = make_agent(tmp_path, MAX_CONCURRENT_PODS=1)
    await agent._sync([pod("a")])
    agent._shippers["a"].finish()

    await agent._sync([pod("a"), pod("b")])
    assert set(agent._shippers) == {"a", "b"}
    assert agent._shippers["b"].started


# ── resilience ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_discovery_failure_changes_nothing_and_retries(tmp_path, monkeypatch):
    agent = make_agent(tmp_path)
    await agent._checkpoints.load()
    await agent._sync([pod("a")])

    async def boom(config):
        raise CrictlError("crictl exploded")

    monkeypatch.setattr(agent_module, "list_chute_pods", boom)
    await agent._poll()  # must not raise
    assert set(agent._shippers) == {"a"}, "a failed poll must not stop live captures"


@pytest.mark.asyncio
async def test_poll_reconciles_checkpoints_to_the_live_pods(tmp_path, monkeypatch):
    agent = make_agent(tmp_path)
    await agent._checkpoints.load()
    await agent._checkpoints.set("gone", {"1": 10})
    await agent._checkpoints.set("a", {"1": 20})

    async def pods(config):
        return [pod("a")]

    monkeypatch.setattr(agent_module, "list_chute_pods", pods)
    await agent._poll()
    assert set(agent._checkpoints.snapshot()) == {"a"}


@pytest.mark.asyncio
async def test_stop_all_stops_every_shipper(tmp_path):
    agent = make_agent(tmp_path)
    await agent._sync([pod("a"), pod("b")])
    shippers = list(agent._shippers.values())
    await agent._stop_all()
    assert all(s.stopped for s in shippers)
    assert agent._shippers == {}


@pytest.mark.asyncio
async def test_run_stops_shippers_when_cancelled(tmp_path, monkeypatch):
    """Shutdown must not leave capture tasks orphaned."""
    agent = make_agent(tmp_path)

    async def pods(config):
        return [pod("a")]

    monkeypatch.setattr(agent_module, "list_chute_pods", pods)
    monkeypatch.setattr(agent_module, "build_ssl_context", lambda config: None)

    task = asyncio.create_task(agent.run())
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert FakeShipper.instances and all(s.stopped for s in FakeShipper.instances)
