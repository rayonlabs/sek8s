"""Behavioural contract for the log shipper — asserts WHAT is shipped, not how.

This suite is the diff harness for the shipper rewrite. It drives the public entry
point (``PodLogShipper.run``) against a fixture pod-log directory and a fake
validator, and asserts only on the POSTed payloads and the loop's terminal
conditions. Nothing here touches an internal method or attribute, so the same file
must pass unchanged before and after the rewrite.

Anything asserted here is frozen behaviour. Anything absent is free to change.
"""

import asyncio
from pathlib import Path

import pytest

from sek8s.log_shipper.checkpoint import CheckpointStore
from sek8s.log_shipper.config import LogShipperConfig
from sek8s.log_shipper.models import ChutePod
from sek8s.log_shipper.shipper import PodLogShipper

STOP = 204  # validator's "capture is over" code


# ── harness ─────────────────────────────────────────────────────────────────


class Validator:
    """Fake validator. Records every shipped batch; replies from a script."""

    def __init__(self, script=None):
        self._script = list(script or [])
        self.batches: list[list[dict]] = []
        self.bodies: list[dict] = []

    @property
    def lines(self) -> list[str]:
        """Every log message shipped, in ship order."""
        return [line["log"] for batch in self.batches for line in batch]

    def post(self, url, json=None, timeout=None):
        self.bodies.append(json)
        self.batches.append(json["logs"])
        status = self._script.pop(0) if self._script else 200  # default: keep going
        return _Post(status)


class _Post:
    def __init__(self, status):
        self._status = status

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    @property
    def status(self):
        return self._status


def config(tmp_path, **overrides) -> LogShipperConfig:
    base = {
        "POD_LOG_ROOT": str(tmp_path),
        "CHECKPOINT_PATH": str(tmp_path / "ckpt.json"),
        "POLL_INTERVAL_SECONDS": 0.01,
        "RETRY_BASE_DELAY_SECONDS": 0.001,
        "RETRY_MAX_DELAY_SECONDS": 0.001,
    }
    base.update(overrides)
    return LogShipperConfig(**base)


def pod() -> ChutePod:
    return ChutePod(
        config_id="cfg-1",
        name="chute-pod",
        uid="uid-1",
        namespace="chutes",
        deployment_id="dep-1",
    )


def write_log(root: Path, name: str, messages, tag="F", start=0) -> Path:
    """Write CRI-framed lines to <pod_dir>/chute/<name>."""
    directory = root / pod().log_dir_name / "chute"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_text(
        "".join(
            f"2026-01-01T00:00:{i + start:02d}.000000000Z stdout {tag} {m}\n"
            for i, m in enumerate(messages)
        )
    )
    return path


async def ship(cfg, validator, checkpoints=None, settle=0.1):
    """Run a shipper until it goes quiet, then stop it.

    The shipper is a daemon: it only ends on its own when the validator replies with
    the stop code, and that can only be delivered in reply to a POST. So a shipper
    that has drained everything simply idles. We wait until no new batch has arrived
    for `settle`, then stop it — which is also what the agent does when a pod goes
    away. Returns (checkpoints, stopped_by_validator).

    This is the only place the contract touches a constructor; every assertion below
    is about what reaches the validator.
    """
    if checkpoints is None:
        checkpoints = CheckpointStore(Path(cfg.checkpoint_path))
        await checkpoints.load()
    shipper = PodLogShipper(pod(), cfg, validator, checkpoints)
    shipper.start()

    seen = -1
    while len(validator.batches) != seen and shipper.running:
        seen = len(validator.batches)
        await asyncio.sleep(settle)

    stopped = not shipper.running
    await shipper.stop()
    return checkpoints, stopped


# ── the contract ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_ships_every_line_in_order_then_stops(tmp_path):
    """The base case: all lines reach the validator, in file order, once."""
    write_log(tmp_path, "0.log", ["a", "b", "c"])
    v = Validator()
    await ship(config(tmp_path), v)
    assert v.lines == ["a", "b", "c"]


@pytest.mark.asyncio
async def test_stop_code_ends_capture(tmp_path):
    """Termination is the validator's decision, not a guest-side rule."""
    write_log(tmp_path, "0.log", ["a"])
    v = Validator(script=[STOP])
    _, stopped = await ship(config(tmp_path), v)
    assert stopped, "the stop code must end run() on its own, without cancellation"
    assert len(v.batches) == 1


@pytest.mark.asyncio
async def test_restart_resumes_and_does_not_reship(tmp_path):
    """Committed progress survives a restart — the point of the checkpoint."""
    log = write_log(tmp_path, "0.log", ["a", "b"])
    cfg = config(tmp_path)

    first = Validator()
    store, _ = await ship(cfg, first)
    assert first.lines == ["a", "b"]

    # Same checkpoint file, new shipper, one new line appended.
    with log.open("a") as fh:
        fh.write("2026-01-01T00:00:09.000000000Z stdout F c\n")
    second = Validator()
    await ship(cfg, second, checkpoints=store)
    assert second.lines == ["c"], "a restart must not re-ship committed lines"


@pytest.mark.asyncio
async def test_multi_chunk_line_is_reassembled(tmp_path):
    """CRI splits a long line into P chunks terminated by one F."""
    directory = tmp_path / pod().log_dir_name / "chute"
    directory.mkdir(parents=True)
    (directory / "0.log").write_text(
        "2026-01-01T00:00:00.000000000Z stdout P he\n"
        "2026-01-01T00:00:00.000000001Z stdout P ll\n"
        "2026-01-01T00:00:00.000000002Z stdout F o\n"
    )
    v = Validator()
    await ship(config(tmp_path), v)
    assert v.lines == ["hello"]


@pytest.mark.asyncio
async def test_incomplete_trailing_line_is_not_shipped(tmp_path):
    """A P-run with no F yet is not a line — it must wait, not ship a fragment."""
    directory = tmp_path / pod().log_dir_name / "chute"
    directory.mkdir(parents=True)
    (directory / "0.log").write_text(
        "2026-01-01T00:00:00.000000000Z stdout F done\n"
        "2026-01-01T00:00:01.000000000Z stdout P partial\n"
    )
    v = Validator()
    await ship(config(tmp_path), v)
    assert v.lines == ["done"]


@pytest.mark.asyncio
async def test_rotated_file_ships_before_current(tmp_path):
    """kubelet rotation: oldest first, so the stream stays time-ordered."""
    write_log(tmp_path, "0.log.20260101-01", ["old1", "old2"])
    write_log(tmp_path, "0.log", ["new1"], start=30)
    v = Validator()
    await ship(config(tmp_path), v)
    assert v.lines == ["old1", "old2", "new1"]


@pytest.mark.asyncio
async def test_oversized_line_still_makes_progress(tmp_path):
    """A logical line larger than the read bound must not stall the pod forever.

    The offset only advances on a shipped line, so refusing to ship it means this
    pod never ships again — an absorbing state, not a delay.
    """
    directory = tmp_path / pod().log_dir_name / "chute"
    directory.mkdir(parents=True)
    chunk = "x" * 4_000
    directory.joinpath("0.log").write_text(
        "".join(
            f"2026-01-01T00:00:{i:02d}.000000000Z stdout P {chunk}\n" for i in range(40)
        )
    )
    v = Validator()
    await ship(config(tmp_path, BATCH_MAX_BYTES=65_536, MAX_LINE_BYTES=1_024), v)
    assert v.lines, "an unterminatable line must still be shipped, truncated"
    assert len(v.lines[0]) <= 1_024


@pytest.mark.asyncio
async def test_oversized_payload_is_split_not_dropped(tmp_path):
    """A 413 splits the batch; every line still arrives."""
    write_log(tmp_path, "0.log", ["a", "b", "c", "d"])
    v = Validator(script=[413])
    await ship(config(tmp_path, BATCH_MAX_LINES=4), v)
    assert set(v.lines) == {"a", "b", "c", "d"}, f"lines lost in the split: {v.lines}"


@pytest.mark.asyncio
async def test_transient_failure_resends_without_gaps(tmp_path):
    """A failed ship commits nothing, so the next cycle resends the same lines."""
    write_log(tmp_path, "0.log", ["a", "b"])
    v = Validator(script=[500, 500])
    await ship(config(tmp_path, RETRY_MAX_ATTEMPTS=2), v)
    assert set(v.lines) == {"a", "b"}, "no line may be lost to a transient failure"


@pytest.mark.asyncio
async def test_no_logs_ships_nothing(tmp_path):
    """An idle pod produces no traffic at all."""
    (tmp_path / pod().log_dir_name / "chute").mkdir(parents=True)
    v = Validator()
    await ship(config(tmp_path), v)
    assert v.batches == []


@pytest.mark.asyncio
async def test_every_shipment_carries_the_deployment_id(tmp_path):
    """The validator correlates on deployment_id; a wrong or missing one breaks it silently.

    Deployment id comes from the miner while instance id comes from the API, so this is
    the field that ties a shipped line back to a deployment API-side. It is pushed into
    every stored record, so it must be present on every batch, not just the first.
    """
    write_log(tmp_path, "0.log", ["a", "b", "c"])
    v = Validator()
    await ship(config(tmp_path, BATCH_MAX_LINES=1), v)
    assert len(v.bodies) >= 2, "want several batches to prove it is not just the first"
    assert all(body["deployment_id"] == pod().deployment_id for body in v.bodies)


@pytest.mark.asyncio
async def test_cancellation_mid_capture_leaves_resumable_state(tmp_path):
    """A pod vanishing is a cancel, not a protocol event — no end signal is sent.

    The validator derives termination from its own launch-config state per batch and
    keeps no "shipper is alive" state, so the guest owes it nothing on the way out.
    What the guest does owe is a checkpoint a later shipper can resume from: committed
    lines are not re-sent, uncommitted ones are (at-least-once).
    """
    log = write_log(tmp_path, "0.log", ["a", "b"])
    cfg = config(tmp_path)

    first = Validator()
    store, stopped = await ship(cfg, first)
    assert not stopped, "no stop code was sent; the agent cancelled it"
    assert first.lines == ["a", "b"]

    with log.open("a") as fh:
        fh.write("2026-01-01T00:00:09.000000000Z stdout F c\n")

    second = Validator()
    await ship(cfg, second, checkpoints=store)
    assert second.lines == [
        "c"
    ], "cancellation must not lose or replay committed progress"
