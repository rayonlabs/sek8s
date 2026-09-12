"""The bytes-to-LogLine path must never raise.

This is what makes the shipper's journal safe to expose. Its journal is miner-readable
through the status API (`SERVICE_ALLOWLIST`) and its input is tenant log content, so the
question is whether that content can ever reach a log line. It cannot reach one directly
-- no call site formats it -- and it cannot reach one indirectly, because an exception
message can only quote input if an exception is raised, and this path raises nothing.

That totality is deliberate, not incidental: `reader.py:66` and `:91` decode with
`errors="ignore"` / `errors="replace"`, `truncate_bytes` is str->str, and `parse_cri_line`
is a `split`. Nothing in the file says those choices are load-bearing, so a later strict
decode or a `json.loads` over a log line would silently create the hole. This fails first
if that happens.
"""

import random
import string

import pytest

from sek8s.log_shipper.reader import parse_cri_line, parse_logical_lines, truncate_bytes

MAX_LINE = 4096


def _cri(ts: str, stream: str, tag: str, message: bytes) -> bytes:
    return f"{ts} {stream} {tag} ".encode() + message + b"\n"


CORPUS = [
    b"",
    b"\n",
    b"\xff\xfe\xfd",  # invalid UTF-8
    b"not-a-cri-line\n",
    b" " * 200 + b"\n",
    _cri("2026-07-27T00:00:00.000000000Z", "stdout", "F", b"\xff\xfe bad utf8"),
    _cri("2026-07-27T00:00:00.000000000Z", "stdout", "P", b"no terminator ever"),
    _cri(
        "2026-07-27T00:00:00.000000000Z", "stderr", "F", b"\x00\x01\x02 control bytes"
    ),
    _cri("", "", "", b"empty fields"),
    _cri("2026-07-27T00:00:00.000000000Z", "stdout", "F", b"x" * (MAX_LINE * 3)),
    b"2026-07-27T00:00:00.000000000Z stdout",  # truncated mid-header
    b"\xed\xa0\x80",  # lone surrogate
]


@pytest.mark.parametrize("data", CORPUS, ids=range(len(CORPUS)))
@pytest.mark.parametrize("flush", [False, True])
def test_parse_logical_lines_never_raises(data, flush):
    parse_logical_lines(data, MAX_LINE, flush_incomplete=flush)


def test_parse_logical_lines_never_raises_on_random_bytes():
    rng = random.Random(20260910)
    for _ in range(400):
        blob = bytes(rng.randrange(256) for _ in range(rng.randrange(0, 300)))
        parse_logical_lines(blob, MAX_LINE)
        parse_logical_lines(blob, MAX_LINE, flush_incomplete=True)


def test_truncate_bytes_is_total_and_returns_str():
    rng = random.Random(20260910)
    for _ in range(200):
        text = "".join(rng.choice(string.printable + "\ud800�ÿ") for _ in range(60))
        for limit in (0, 1, 7, 64, 4096):
            assert isinstance(truncate_bytes(text, limit), str)


def test_parse_cri_line_is_total():
    for raw in ("", " ", "a b", "a b c", "a b c d e", "\x00", "  \t  "):
        parse_cri_line(raw)
