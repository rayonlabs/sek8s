"""Unit tests for the log shipper's support modules.

The reader, shipper and agent have their own files —
test_log_shipper_reader.py, test_log_shipper_shipper.py, test_log_shipper_agent.py.
What is left here is config, models, the checkpoint store, the crictl wrapper and the
service entrypoint.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from sek8s.log_shipper.checkpoint import CheckpointStore
from sek8s.log_shipper.config import LogShipperConfig
from sek8s.log_shipper.crictl import (
    CrictlError,
    list_chute_pods,
    parse_chute_pods,
    run_crictl,
)
from sek8s.log_shipper.models import ChutePod

# ── Helpers ─────────────────────────────────────────────────────────────────


def make_config(**overrides) -> LogShipperConfig:
    """Build a config with fast timings; overrides use env aliases (no populate_by_name)."""
    base = {
        "POLL_INTERVAL_SECONDS": 0.01,
        "RETRY_MAX_ATTEMPTS": 2,
        "RETRY_BASE_DELAY_SECONDS": 0.001,
        "RETRY_MAX_DELAY_SECONDS": 0.001,
    }
    base.update(overrides)
    return LogShipperConfig(**base)


def make_pod(**overrides) -> ChutePod:
    fields = dict(
        config_id="cfg",
        name="pod",
        uid="uid",
        namespace="chutes",
        deployment_id="dep-1",
    )
    fields.update(overrides)
    return ChutePod(**fields)


async def make_checkpoints(tmp_path) -> CheckpointStore:
    store = CheckpointStore(tmp_path / "checkpoint.json")
    await store.load()
    return store


def cri(ts, msg, stream="stdout", tag="F") -> str:
    return f"{ts} {stream} {tag} {msg}"


def write_log_file(root, pod, container, name, lines) -> "object":
    directory = root / pod.log_dir_name / container
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_text("".join(line + "\n" for line in lines))
    return path


class FakeResp:
    def __init__(self, status: int):
        self.status = status

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class FakePost:
    def __init__(self, outcome):
        self._outcome = outcome  # int status or Exception

    async def __aenter__(self):
        if isinstance(self._outcome, Exception):
            raise self._outcome
        return FakeResp(self._outcome)

    async def __aexit__(self, *exc):
        return False


class FakeSession:
    """Minimal stand-in for aiohttp.ClientSession.post."""

    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = []

    def post(self, url, json=None, timeout=None):
        self.calls.append({"url": url, "json": json})
        outcome = self.outcomes.pop(0) if self.outcomes else 200
        return FakePost(outcome)


# ── config.py ───────────────────────────────────────────────────────────────


def test_config_defaults(monkeypatch):
    monkeypatch.delenv("VALIDATOR_BASE_URL", raising=False)
    config = LogShipperConfig()
    assert (
        config.logs_url("abc")
        == "https://cvm.chutes.ai/instances/launch_config/abc/logs"
    )
    assert config.selector_key == "chutes/chute"
    assert config.selector_value == "true"
    assert config.stop_status_code == 204
    assert config.terminal_status_codes == [403, 404]
    assert config.deployment_id_label == "chutes/deployment-id"
    assert config.batch_max_bytes == 1_048_576
    assert config.checkpoint_path.name == "checkpoint.json"
    assert config.container_name == "chute"


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("[401, 410]", [401, 410]),
        ("401,410", [401, 410]),
        ("403", [403]),
        ([401, 402], [401, 402]),
    ],
)
def test_terminal_status_codes_parsing(raw, expected):
    config = LogShipperConfig(TERMINAL_STATUS_CODES=raw)
    assert config.terminal_status_codes == expected


def test_logs_url_strips_trailing_slash():
    config = LogShipperConfig(VALIDATOR_BASE_URL="https://cvm.chutes.ai/")
    assert (
        config.logs_url("x") == "https://cvm.chutes.ai/instances/launch_config/x/logs"
    )


@pytest.mark.parametrize("bad", ["noequals", "=value", "key="])
def test_invalid_label_selector_rejected(bad):
    with pytest.raises(ValueError):
        LogShipperConfig(LABEL_SELECTOR=bad)


# ── models.py ───────────────────────────────────────────────────────────────


def test_chute_pod_log_dir_name():
    pod = ChutePod(config_id="c", name="pod-a", uid="uid-1", namespace="chutes")
    assert pod.log_dir_name == "chutes_pod-a_uid-1"


# ── checkpoint.py ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_checkpoint_missing_file_loads_empty(tmp_path):
    store = CheckpointStore(tmp_path / "nope" / "checkpoint.json")
    await store.load()
    assert store.get("x") == {}


@pytest.mark.asyncio
async def test_checkpoint_corrupt_and_non_dict_load_empty(tmp_path):
    path = tmp_path / "checkpoint.json"
    path.write_text("not json{{")
    store = CheckpointStore(path)
    await store.load()
    assert store.snapshot() == {}
    path.write_text("[1, 2, 3]")
    store2 = CheckpointStore(path)
    await store2.load()
    assert store2.snapshot() == {}


@pytest.mark.asyncio
async def test_checkpoint_set_get_persists(tmp_path):
    path = tmp_path / "sub" / "checkpoint.json"
    store = CheckpointStore(path)
    await store.load()
    await store.set("c1", {"7": 100, "8": 200})
    assert store.get("c1") == {"7": 100, "8": 200}
    # get returns a copy — mutating it must not affect the store.
    store.get("c1")["7"] = 0
    assert store.get("c1")["7"] == 100
    reloaded = CheckpointStore(path)
    await reloaded.load()
    assert reloaded.get("c1") == {"7": 100, "8": 200}


@pytest.mark.asyncio
async def test_checkpoint_evict_and_reconcile(tmp_path):
    store = CheckpointStore(tmp_path / "checkpoint.json")
    await store.load()
    await store.set("a", {"1": 1})
    await store.set("b", {"1": 2})
    await store.set("c", {"1": 3})
    await store.evict("a")
    assert store.get("a") == {}
    removed = await store.reconcile({"b"})
    assert removed == 1
    assert store.get("c") == {}
    assert store.get("b") == {"1": 2}
    await store.evict("missing")  # no-op


# ── crictl.py ───────────────────────────────────────────────────────────────


def _pods_json(items):
    return json.dumps({"items": items})


def test_parse_chute_pods_filters():
    config = make_config()
    raw = _pods_json(
        [
            {
                "metadata": {"name": "good", "uid": "u1", "namespace": "chutes"},
                "state": "SANDBOX_READY",
                "labels": {
                    "chutes/chute": "true",
                    "chutes/config-id": "cfg-1",
                    "chutes/deployment-id": "dep-1",
                },
            },
            {  # wrong namespace
                "metadata": {"name": "n", "uid": "u2", "namespace": "default"},
                "labels": {"chutes/chute": "true", "chutes/config-id": "cfg-2"},
            },
            {  # selector mismatch
                "metadata": {"name": "n", "uid": "u3", "namespace": "chutes"},
                "labels": {"chutes/chute": "false", "chutes/config-id": "cfg-3"},
            },
            {  # missing config-id label
                "metadata": {"name": "n", "uid": "u4", "namespace": "chutes"},
                "labels": {"chutes/chute": "true"},
            },
            {  # missing uid
                "metadata": {"name": "n", "namespace": "chutes"},
                "labels": {"chutes/chute": "true", "chutes/config-id": "cfg-5"},
            },
        ]
    )
    pods = parse_chute_pods(raw, config)
    assert len(pods) == 1
    assert pods[0].config_id == "cfg-1"
    assert pods[0].deployment_id == "dep-1"
    assert pods[0].state == "SANDBOX_READY"


def test_parse_chute_pods_missing_deployment_id_defaults_empty():
    raw = _pods_json(
        [
            {
                "metadata": {"name": "n", "uid": "u", "namespace": "chutes"},
                "labels": {"chutes/chute": "true", "chutes/config-id": "cfg"},
            }
        ]
    )
    pods = parse_chute_pods(raw, make_config())
    assert pods[0].deployment_id == ""


def test_parse_chute_pods_empty_and_bad():
    assert parse_chute_pods(json.dumps({}), make_config()) == []
    with pytest.raises(CrictlError):
        parse_chute_pods("not json", make_config())


class FakeProc:
    def __init__(self, stdout=b"", stderr=b"", returncode=0, hang=False):
        self._stdout = stdout
        self._stderr = stderr
        self.returncode = returncode
        self._hang = hang
        self.killed = False

    async def communicate(self):
        if self._hang:
            await asyncio.sleep(10)
        return self._stdout, self._stderr

    def kill(self):
        self.killed = True


@pytest.mark.asyncio
async def test_run_crictl_success(monkeypatch):
    async def fake_exec(*args, **kwargs):
        return FakeProc(stdout=b'{"items": []}')

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    assert await run_crictl(make_config(), ["pods", "-o", "json"]) == '{"items": []}'


@pytest.mark.asyncio
async def test_run_crictl_nonzero_exit(monkeypatch):
    async def fake_exec(*args, **kwargs):
        return FakeProc(stderr=b"boom", returncode=1)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    with pytest.raises(CrictlError, match="exited 1"):
        await run_crictl(make_config(), ["pods"])


@pytest.mark.asyncio
async def test_run_crictl_missing_wrapper(monkeypatch):
    async def fake_exec(*args, **kwargs):
        raise FileNotFoundError()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    with pytest.raises(CrictlError, match="not found"):
        await run_crictl(make_config(), ["pods"])


@pytest.mark.asyncio
async def test_run_crictl_timeout(monkeypatch):
    proc = FakeProc(hang=True)

    async def fake_exec(*args, **kwargs):
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    with pytest.raises(CrictlError, match="timed out"):
        await run_crictl(make_config(COMMAND_TIMEOUT_SECONDS=0.01), ["pods"])
    assert proc.killed


@pytest.mark.asyncio
async def test_list_chute_pods(monkeypatch):
    raw = _pods_json(
        [
            {
                "metadata": {"name": "p", "uid": "u", "namespace": "chutes"},
                "labels": {"chutes/chute": "true", "chutes/config-id": "cfg"},
            }
        ]
    )

    async def fake_exec(*args, **kwargs):
        return FakeProc(stdout=raw.encode())

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    pods = await list_chute_pods(make_config())
    assert [p.config_id for p in pods] == ["cfg"]


# ── services/log_shipper.py entrypoint ───────────────────────────────────────


def test_service_run_invokes_agent(monkeypatch):
    import sek8s.services.log_shipper as entry

    started = {"ran": False}

    class FakeAgent:
        def __init__(self, config):
            pass

        async def run(self):
            started["ran"] = True

    monkeypatch.setattr(entry, "LogShipperAgent", FakeAgent)
    entry.run()
    assert started["ran"] is True


def test_service_run_handles_keyboard_interrupt(monkeypatch):
    import sek8s.services.log_shipper as entry

    def fake_run(coro):
        coro.close()
        raise KeyboardInterrupt()

    monkeypatch.setattr(entry.asyncio, "run", fake_run)
    entry.run()  # must not raise
