import asyncio
import contextlib

"""Unit tests for reader.py — turning CRI log files into complete lines."""

from pathlib import Path

import pytest

from sek8s.log_shipper.checkpoint import CheckpointStore
from sek8s.log_shipper.config import LogShipperConfig
from sek8s.log_shipper.models import ChutePod
from sek8s.log_shipper.reader import (
    PodLogReader,
    log_files,
    log_sort_key,
    parse_cri_line,
    parse_logical_lines,
    truncate_bytes,
)

POD = ChutePod(
    config_id="cfg-1",
    name="chute-pod",
    uid="uid-1",
    namespace="chutes",
    deployment_id="dep-1",
)


def cri(ts, msg, stream="stdout", tag="F") -> str:
    return f"{ts} {stream} {tag} {msg}\n"


async def make_reader(tmp_path, **overrides) -> PodLogReader:
    base = {
        "POD_LOG_ROOT": str(tmp_path),
        "CHECKPOINT_PATH": str(tmp_path / "ckpt.json"),
        "POLL_INTERVAL_SECONDS": 0.01,
        "DRAIN_INTERVAL_SECONDS": 0.01,
    }
    base.update(overrides)
    config = LogShipperConfig(**base)
    checkpoints = CheckpointStore(Path(config.checkpoint_path))
    await checkpoints.load()
    return PodLogReader(POD, config, checkpoints)


def write(tmp_path, name, text) -> Path:
    directory = tmp_path / POD.log_dir_name / "chute"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_text(text)
    return path


# ── CRI framing ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "raw,expected",
    [
        (
            "2026-01-01T00:00:00Z stdout F hi",
            ("2026-01-01T00:00:00Z", "stdout", "F", "hi"),
        ),
        (
            "2026-01-01T00:00:00Z stderr P chunk",
            ("2026-01-01T00:00:00Z", "stderr", "P", "chunk"),
        ),
        ("2026-01-01T00:00:00Z stdout F", ("2026-01-01T00:00:00Z", "stdout", "F", "")),
        ("", None),
        ("only two", None),
        ("ts badstream F x", None),
        ("ts stdout X x", None),
    ],
)
def test_parse_cri_line(raw, expected):
    assert parse_cri_line(raw) == expected


def test_truncate_bytes_is_byte_bounded_not_char_bounded():
    assert truncate_bytes("abc", 10) == "abc"
    assert truncate_bytes("a" * 20, 5) == "aaaaa"
    # A multi-byte char must not be split into invalid UTF-8.
    assert truncate_bytes("é" * 10, 5).encode("utf-8") == "éé".encode("utf-8")


def test_multi_chunk_line_is_reassembled():
    data = (
        cri("t0", "he", tag="P") + cri("t1", "ll", tag="P") + cri("t2", "o")
    ).encode()
    lines = parse_logical_lines(data, 1024)
    assert [line.log for line, _ in lines] == ["hello"]


def test_streams_are_reassembled_independently():
    data = (
        cri("t0", "out-", tag="P")
        + cri("t1", "err-", stream="stderr", tag="P")
        + cri("t2", "done", stream="stderr")
        + cri("t3", "done")
    ).encode()
    lines = parse_logical_lines(data, 1024)
    assert {(line.stream, line.log) for line, _ in lines} == {
        ("stderr", "err-done"),
        ("stdout", "out-done"),
    }


def test_unterminated_run_is_withheld():
    """No F yet means no line — shipping a fragment would be wrong."""
    data = (cri("t0", "done") + cri("t1", "partial", tag="P")).encode()
    lines = parse_logical_lines(data, 1024)
    assert [line.log for line, _ in lines] == ["done"]


def test_trailing_physical_line_without_newline_is_withheld():
    data = (cri("t0", "done") + "2026 stdout F truncated-mid-write").encode()
    lines = parse_logical_lines(data, 1024)
    assert [line.log for line, _ in lines] == ["done"]


def test_offsets_point_just_past_the_terminating_record():
    data = (cri("t0", "a") + cri("t1", "b")).encode()
    lines = parse_logical_lines(data, 1024)
    assert [offset for _, offset in lines] == [len(cri("t0", "a")), len(data)]


def test_flush_incomplete_emits_a_withheld_run():
    data = (cri("t0", "aa", tag="P") + cri("t1", "bb", tag="P")).encode()
    assert parse_logical_lines(data, 1024) == []
    flushed = parse_logical_lines(data, 1024, flush_incomplete=True)
    assert [line.log for line, _ in flushed] == ["aabb"]
    assert flushed[0][1] == len(data), "must consume the whole parsed region"


def test_flush_incomplete_is_ignored_when_a_line_completed():
    """If anything completed, progress is already possible — do not also flush."""
    data = (cri("t0", "done") + cri("t1", "partial", tag="P")).encode()
    assert [
        line.log for line, _ in parse_logical_lines(data, 1024, flush_incomplete=True)
    ] == ["done"]


# ── file discovery ──────────────────────────────────────────────────────────


def test_log_files_are_ordered_oldest_first(tmp_path):
    for name in ["0.log", "1.log", "0.log.20260101-02", "0.log.20260101-01"]:
        write(tmp_path, name, "")
    names = [p.name for p in log_files(tmp_path / POD.log_dir_name, "chute")]
    assert names == ["0.log.20260101-01", "0.log.20260101-02", "0.log", "1.log"]


def test_compressed_and_other_containers_are_skipped(tmp_path):
    write(tmp_path, "0.log", "")
    write(tmp_path, "0.log.20260101-01.gz", "")
    (tmp_path / POD.log_dir_name / "sidecar").mkdir(parents=True)
    (tmp_path / POD.log_dir_name / "sidecar" / "0.log").write_text("")
    names = [p.name for p in log_files(tmp_path / POD.log_dir_name, "chute")]
    assert names == ["0.log"]


def test_missing_directory_is_not_an_error(tmp_path):
    assert log_files(tmp_path / "nope", "chute") == []


def test_unparseable_names_sort_deterministically(tmp_path):
    assert log_sort_key(Path("weird.txt"))[0] == 0


# ── reading ─────────────────────────────────────────────────────────────────


async def drain(reader, *, batches=1, fail_on=None, settle=0.3):
    """Consume `batches` batches from stream(). Returns the non-empty ones.

    `fail_on` raises inside the consumer body on that batch index, which is what a
    failed post looks like to the generator.

    Note the extra request at the end. The commit for batch N runs when the consumer
    asks for N+1 — that is the point of putting it after the yield — so a consumer
    that takes one batch and walks away never commits it. Real shippers ask again;
    tests have to as well.
    """
    got = []
    stream = reader.stream()
    try:
        for i in range(batches):
            got.append(await asyncio.wait_for(anext(stream), timeout=settle))
            if i == fail_on:
                raise RuntimeError("post failed")
        with contextlib.suppress(asyncio.TimeoutError):
            got.append(await asyncio.wait_for(anext(stream), timeout=settle))
    finally:
        await stream.aclose()
    return [batch for batch in got if batch]


@pytest.mark.asyncio
async def test_stream_yields_complete_lines(tmp_path):
    write(tmp_path, "0.log", cri("t0", "a") + cri("t1", "b"))
    reader = await make_reader(tmp_path)
    [batch] = await drain(reader)
    assert [line.log for line in batch] == ["a", "b"]


@pytest.mark.asyncio
async def test_commit_happens_after_the_consumer_succeeds(tmp_path):
    """Two batches: consuming the first commits it, so the second differs."""
    write(tmp_path, "0.log", cri("t0", "a") + cri("t1", "b"))
    reader = await make_reader(tmp_path, BATCH_MAX_LINES=1)
    batches = await drain(reader, batches=2)
    assert [[line.log for line in b] for b in batches] == [["a"], ["b"]]


@pytest.mark.asyncio
async def test_a_failed_consumer_does_not_commit(tmp_path):
    """The property everything rests on: no commit unless the body completed."""
    write(tmp_path, "0.log", cri("t0", "a"))
    reader = await make_reader(tmp_path)
    with pytest.raises(RuntimeError):
        await drain(reader, fail_on=0)
    assert reader._offsets == {}, "a failed post must leave progress untouched"

    # A fresh consumer re-reads the same line.
    [batch] = await drain(reader)
    assert [line.log for line in batch] == ["a"]


@pytest.mark.asyncio
async def test_cancellation_does_not_commit(tmp_path):
    """A pod going away mid-batch must not advance progress either."""
    write(tmp_path, "0.log", cri("t0", "a"))
    reader = await make_reader(tmp_path)

    async def consume():
        async for _ in reader.stream():
            await asyncio.sleep(10)  # cancelled here, before the body finishes

    task = asyncio.create_task(consume())
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert reader._offsets == {}


@pytest.mark.asyncio
async def test_committed_progress_survives_a_new_reader(tmp_path):
    """The restart case: a fresh reader on the same checkpoint resumes."""
    write(tmp_path, "0.log", cri("t0", "a"))
    first = await make_reader(tmp_path)
    await drain(first)  # the trailing request is what commits it

    config = LogShipperConfig(
        POD_LOG_ROOT=str(tmp_path), CHECKPOINT_PATH=str(tmp_path / "ckpt.json")
    )
    checkpoints = CheckpointStore(Path(config.checkpoint_path))
    await checkpoints.load()
    assert PodLogReader(POD, config, checkpoints)._read_next() == []


@pytest.mark.asyncio
async def test_idle_pod_yields_nothing(tmp_path):
    """No logs means the generator waits rather than yielding empty batches."""
    (tmp_path / POD.log_dir_name / "chute").mkdir(parents=True)
    reader = await make_reader(tmp_path)
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(anext(reader.stream()), timeout=0.15)


@pytest.mark.asyncio
async def test_batch_is_bounded_by_lines(tmp_path):
    write(tmp_path, "0.log", "".join(cri(f"t{i}", str(i)) for i in range(10)))
    reader = await make_reader(tmp_path, BATCH_MAX_LINES=3)
    assert len(reader._read_next()) == 3


@pytest.mark.asyncio
async def test_batch_is_bounded_by_bytes(tmp_path):
    write(tmp_path, "0.log", cri("t0", "x" * 5_000) * 10)
    reader = await make_reader(tmp_path, BATCH_MAX_BYTES=8_000)
    assert len(reader._read_next()) < 10, "the byte bound is the memory ceiling"


@pytest.mark.asyncio
async def test_rotated_file_is_read_before_current(tmp_path):
    write(tmp_path, "0.log.20260101-01", cri("t0", "old"))
    write(tmp_path, "0.log", cri("t1", "new"))
    reader = await make_reader(tmp_path)
    assert [line.log for line in reader._read_next()] == ["old", "new"]


@pytest.mark.asyncio
async def test_truncated_file_restarts_from_zero(tmp_path):
    log = write(tmp_path, "0.log", cri("t0", "a") + cri("t1", "b"))
    reader = await make_reader(tmp_path)
    await drain(reader)
    log.write_text(cri("t2", "fresh"))  # truncate + rewrite, same inode
    assert [line.log for line in reader._read_next()] == ["fresh"]


@pytest.mark.asyncio
async def test_line_longer_than_a_batch_is_truncated_not_withheld(tmp_path):
    """The absorbing state: withholding here would stall this pod for good."""
    write(tmp_path, "0.log", cri("t0", "x" * 4_000, tag="P") * 40)
    reader = await make_reader(tmp_path, BATCH_MAX_BYTES=65_536, MAX_LINE_BYTES=1_024)
    batches = await drain(reader)
    assert batches, "a line that can never terminate must still be emitted"
    assert len(batches[0][0].log) <= 1_024
    # More than one batch came out, so the offset really moved rather than
    # re-yielding the same bytes — which is the whole failure this guards.
    assert len(batches) > 1
    assert reader._offsets


@pytest.mark.asyncio
async def test_offsets_survive_a_bounded_read_that_missed_later_files(tmp_path):
    """Do not forget a file just because this read ran out of budget before it.

    Eviction keys on the directory listing, not on what the read reached — otherwise
    a later file's committed offset is dropped and its whole content re-ships.
    """
    write(tmp_path, "0.log.20260101-01", cri("t0", "x" * 100_000))
    later = write(tmp_path, "0.log", cri("t1", "later"))
    reader = await make_reader(tmp_path, BATCH_MAX_BYTES=65_536)

    reader._offsets[later.stat().st_ino] = 12345  # pretend we shipped it earlier
    reader._read_next()
    assert reader._offsets.get(later.stat().st_ino) == 12345


@pytest.mark.asyncio
async def test_offsets_for_deleted_files_are_purged(tmp_path):
    write(tmp_path, "0.log", cri("t0", "a"))
    reader = await make_reader(tmp_path)
    await drain(reader)
    assert reader._offsets

    for path in (tmp_path / POD.log_dir_name / "chute").iterdir():
        path.unlink()
    reader._read_next()
    assert reader._offsets == {}, "a deleted file's offset must not leak forever"


@pytest.mark.asyncio
async def test_missing_pod_directory_yields_nothing(tmp_path):
    reader = await make_reader(tmp_path)
    assert reader._read_next() == []
