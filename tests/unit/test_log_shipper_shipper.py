"""Unit tests for shipper.py — delivering batches until the validator stops us."""

import asyncio

import pytest

from sek8s.log_shipper.checkpoint import CheckpointStore
from sek8s.log_shipper.config import LogShipperConfig
from sek8s.log_shipper.models import ChutePod, LogLine
from sek8s.log_shipper.shipper import PodLogShipper

POD = ChutePod(
    config_id="cfg-1",
    name="chute-pod",
    uid="uid-1",
    namespace="chutes",
    deployment_id="dep-1",
)


# ── fakes ───────────────────────────────────────────────────────────────────


class _Response:
    def __init__(self, status):
        self.status = status

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class Validator:
    """Records posted batches; replies from a script, defaulting to accept."""

    def __init__(self, script=None, always=None):
        self._script = list(script or [])
        self._always = always
        self.posts: list[list[str]] = []

    @property
    def lines(self):
        return [line for post in self.posts for line in post]

    def post(self, url, json=None, timeout=None):
        self.posts.append([line["log"] for line in json["logs"]])
        if self._always is not None:
            return _Response(self._always)
        outcome = self._script.pop(0) if self._script else 200
        if isinstance(outcome, Exception):
            raise outcome
        return _Response(outcome)


class FakeReader:
    """Stands in for PodLogReader: yields scripted batches, records commits."""

    def __init__(self, batches, *, repeat_last=False):
        self._batches = [list(b) for b in batches]
        self._repeat_last = repeat_last
        self.committed: list[list[str]] = []
        self._handed = None

    async def stream(self):
        for batch in self._batches:
            self._handed = batch
            yield [
                LogLine(ts=f"t{i}", stream="stdout", log=m) for i, m in enumerate(batch)
            ]
            self.committed.append(self._handed)
        while self._repeat_last:  # keep the stream alive, like an idle pod
            await asyncio.sleep(0.01)


async def make_shipper(tmp_path, reader, validator, **overrides):
    base = {
        "POD_LOG_ROOT": str(tmp_path),
        "CHECKPOINT_PATH": str(tmp_path / "ckpt.json"),
        "POLL_INTERVAL_SECONDS": 0.01,
        "DRAIN_INTERVAL_SECONDS": 0.01,
        "RETRY_BASE_DELAY_SECONDS": 0.001,
        "RETRY_MAX_DELAY_SECONDS": 0.001,
    }
    base.update(overrides)
    config = LogShipperConfig(**base)
    checkpoints = CheckpointStore(tmp_path / "ckpt.json")
    await checkpoints.load()
    shipper = PodLogShipper(POD, config, validator, checkpoints)
    shipper._reader = reader  # the real reader has its own tests
    return shipper


async def run_to_completion(shipper, timeout=3):
    shipper.start()
    await asyncio.wait_for(shipper._task, timeout=timeout)


# ── delivery ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_ships_each_batch_and_commits_it(tmp_path):
    reader = FakeReader([["a", "b"], ["c"]])
    validator = Validator()
    await run_to_completion(await make_shipper(tmp_path, reader, validator))
    assert validator.posts == [["a", "b"], ["c"]]
    assert reader.committed == [["a", "b"], ["c"]]


@pytest.mark.asyncio
async def test_stop_code_ends_capture(tmp_path):
    """The validator owns termination; the batch it stopped on is not committed."""
    reader = FakeReader([["a"], ["b"]])
    validator = Validator(script=[204])
    shipper = await make_shipper(tmp_path, reader, validator)
    await run_to_completion(shipper)
    assert validator.posts == [["a"]]
    assert reader.committed == [], "a stopped batch was never accepted"
    assert not shipper.running


@pytest.mark.asyncio
async def test_terminal_rejection_ends_capture(tmp_path):
    reader = FakeReader([["a"], ["b"]])
    validator = Validator(script=[403])
    shipper = await make_shipper(tmp_path, reader, validator)
    await run_to_completion(shipper)
    assert validator.posts == [["a"]]
    assert reader.committed == []


@pytest.mark.asyncio
async def test_oversized_batch_is_split_until_it_lands(tmp_path):
    """413 halves the batch; every line still arrives, and only then commits."""
    reader = FakeReader([["a", "b", "c", "d"]])
    validator = Validator(script=[413])
    await run_to_completion(await make_shipper(tmp_path, reader, validator))
    assert validator.posts == [["a", "b", "c", "d"], ["a", "b"], ["c", "d"]]
    assert reader.committed == [["a", "b", "c", "d"]], "commit once all halves land"


@pytest.mark.asyncio
async def test_a_single_line_the_validator_refuses_is_dropped(tmp_path):
    """Rather than wedge the pod on one line that can never be delivered."""
    reader = FakeReader([["huge"]])
    validator = Validator(script=[413])
    await run_to_completion(await make_shipper(tmp_path, reader, validator))
    assert reader.committed == [["huge"]], "progress continues past the bad line"


@pytest.mark.asyncio
async def test_retries_a_transient_status_then_succeeds(tmp_path):
    reader = FakeReader([["a"]])
    validator = Validator(script=[500, 502])
    await run_to_completion(
        await make_shipper(tmp_path, reader, validator, RETRY_MAX_ATTEMPTS=3)
    )
    assert len(validator.posts) == 3
    assert reader.committed == [["a"]]


@pytest.mark.asyncio
async def test_exhausted_retries_do_not_commit(tmp_path):
    """The property the generator design depends on: a failed post commits nothing.

    Catching this inside the `async for` body would hand control back to the reader,
    which commits — a batch the validator never accepted.
    """
    reader = FakeReader([["a"]], repeat_last=True)
    validator = Validator(always=500)  # never accepts, so the retry cannot rescue it
    shipper = await make_shipper(tmp_path, reader, validator, RETRY_MAX_ATTEMPTS=2)
    shipper.start()
    await asyncio.sleep(0.15)
    await shipper.stop()
    assert validator.posts, "it did try"
    assert reader.committed == [], "nothing may commit after a failed ship"


# ── lifecycle ───────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_start_is_idempotent_while_running(tmp_path):
    reader = FakeReader([["a"]], repeat_last=True)
    shipper = await make_shipper(tmp_path, reader, Validator())
    shipper.start()
    first = shipper._task
    shipper.start()
    assert shipper._task is first, "a second start must not spawn a rival task"
    await shipper.stop()


@pytest.mark.asyncio
async def test_running_is_false_once_the_validator_stops_us(tmp_path):
    """This is what stops the agent respawning a pod that is still present."""
    reader = FakeReader([["a"]])
    shipper = await make_shipper(tmp_path, reader, Validator(script=[204]))
    assert not shipper.running
    await run_to_completion(shipper)
    assert not shipper.running


@pytest.mark.asyncio
async def test_stop_cancels_and_waits(tmp_path):
    reader = FakeReader([["a"]], repeat_last=True)
    shipper = await make_shipper(tmp_path, reader, Validator())
    shipper.start()
    await asyncio.sleep(0.05)
    await shipper.stop()
    assert not shipper.running
    assert shipper._task is None


@pytest.mark.asyncio
async def test_stop_before_start_is_harmless(tmp_path):
    shipper = await make_shipper(tmp_path, FakeReader([]), Validator())
    await shipper.stop()
    assert not shipper.running


@pytest.mark.asyncio
async def test_an_unexpected_error_is_surfaced_not_swallowed(tmp_path):
    """An unretrieved task exception would otherwise vanish into a GC warning."""

    class Exploding:
        async def stream(self):
            raise ValueError("boom")
            yield  # pragma: no cover - makes this an async generator

    shipper = await make_shipper(tmp_path, Exploding(), Validator())
    await run_to_completion(shipper)
    assert isinstance(shipper.error, ValueError)
    assert not shipper.running


@pytest.mark.asyncio
async def test_cancellation_propagates_rather_than_being_recorded_as_an_error(tmp_path):
    reader = FakeReader([["a"]], repeat_last=True)
    shipper = await make_shipper(tmp_path, reader, Validator())
    shipper.start()
    await asyncio.sleep(0.05)
    await shipper.stop()
    assert shipper.error is None, "a normal stop is not a failure"


@pytest.mark.asyncio
async def test_a_transient_failure_resumes_from_the_last_commit(tmp_path):
    """After retries are spent the stream is torn down, then a new one re-reads.

    The uncommitted batch comes round again and lands on the retry, so a validator
    blip costs a re-send rather than the pod's logs.
    """
    reader = FakeReader([["a"]])
    validator = Validator(script=[500, 500])  # both attempts of the first pass fail
    shipper = await make_shipper(tmp_path, reader, validator, RETRY_MAX_ATTEMPTS=2)
    await run_to_completion(shipper)
    assert validator.posts == [["a"], ["a"], ["a"]], "two failed, then one that landed"
    assert reader.committed == [["a"]], "committed exactly once, after it landed"


def test_rejection_reason_names_the_cause():
    """The reason is logged when capture ends, so it has to say something useful."""
    from sek8s.log_shipper.shipper import _rejection_reason

    assert _rejection_reason(404) == "unknown config_id"
    assert _rejection_reason(403) == "cert/ownership rejected"
    assert _rejection_reason(410) == "rejected"
