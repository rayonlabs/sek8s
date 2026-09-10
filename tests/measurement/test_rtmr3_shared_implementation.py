"""The RTMR3 measurement has exactly ONE implementation — these tests hold it there.

Four independent walkers of ``tdx-measure.conf`` used to exist (the initramfs measurer,
the build-time manifest generator, ``rtmr3-verify`` and the host-side predictor) and they
disagreed four ways. Each divergence below has a test named for the finding it closes, so
a regression reads as "we grew a second implementation again" rather than as a hash bug.

Everything here drives the real bundled ``tdx-measure`` script — no reimplementation of
the walk in test code, because that is precisely the mistake being guarded against.
"""

import hashlib
import subprocess

import pytest
from chutes_cvm.measurement.rtmr3 import Rtmr3Error, fold_chain, measured_hashes
from chutes_cvm.paths import tdx_measure_script


def _run(command, root, conf):
    return subprocess.run(
        [str(tdx_measure_script()), command, str(root), str(conf)],
        capture_output=True,
        text=True,
    )


@pytest.fixture
def fixture_root(tmp_path):
    """A miniature measured root plus a conf naming parts of it."""
    root = tmp_path / "root"
    (root / "etc/ssh").mkdir(parents=True)
    (root / "usr/local/bin").mkdir(parents=True)
    (root / "etc/ssh/sshd_config").write_text("cfg")
    (root / "etc/ssh/moduli").write_text("mod")
    (root / "etc/single.conf").write_text("single")
    (root / "usr/local/bin/tool").write_text("tool")
    conf = tmp_path / "conf"
    conf.write_text("/etc/ssh\n/etc/single.conf\n/usr/local/bin\n")
    return root, conf


def test_hash_and_list_agree_on_order(fixture_root):
    """`hash` must emit the same files in the same order as `list` — the chain depends on it."""
    root, conf = fixture_root
    listed = _run("list", root, conf).stdout.splitlines()
    hashed = [
        line.split(" ", 1)[1] for line in _run("hash", root, conf).stdout.splitlines()
    ]
    assert listed == hashed


def test_inline_comments_and_trailing_whitespace_are_stripped(tmp_path):
    """The build walker dropped only whole-line comments, so an inline comment
    or trailing space silently removed a file from the canonical manifest."""
    root = tmp_path / "root"
    (root / "etc").mkdir(parents=True)
    (root / "etc/a").write_text("a")
    (root / "etc/b").write_text("b")
    conf = tmp_path / "conf"
    # inline comment, trailing spaces, a trailing tab, and a CRLF line ending
    conf.write_bytes(b"/etc/a   # why this file\n/etc/b\t  \r\n# whole-line\n\n")

    rels = [rel for _, rel in measured_hashes(root, conf, tdx_measure_script())]
    assert rels == ["/etc/a", "/etc/b"]


def test_symlinked_file_entry_is_refused(tmp_path):
    """`[ -f ]` follows symlinks and `is_symlink()` rejects them, so the shell
    walkers measured a symlinked entry that the Python ones skipped."""
    root = tmp_path / "root"
    (root / "etc").mkdir(parents=True)
    (root / "etc/real").write_text("x")
    (root / "etc/link").symlink_to(root / "etc/real")
    conf = tmp_path / "conf"
    conf.write_text("/etc/real\n/etc/link\n")

    assert _run("hash", root, conf).returncode == 1
    with pytest.raises(Rtmr3Error, match="symlink"):
        measured_hashes(root, conf, tdx_measure_script())


def test_symlinked_directory_entry_is_refused(tmp_path):
    """The severe half: `find -P` will not descend a symlinked directory while
    `rglob` will, so the authoritative boot measurer contributed ZERO files for such an
    entry while the verifier expected the whole tree."""
    root = tmp_path / "root"
    (root / "etc/real").mkdir(parents=True)
    (root / "etc/real/f").write_text("x")
    (root / "etc/link").symlink_to(root / "etc/real")
    conf = tmp_path / "conf"
    conf.write_text("/etc/link\n")

    assert _run("hash", root, conf).returncode == 1
    with pytest.raises(Rtmr3Error, match="symlink"):
        measured_hashes(root, conf, tdx_measure_script())


def test_symlink_inside_a_measured_directory_is_skipped(tmp_path):
    """The other side of the same coin: symlinks FOUND under a measured directory are
    correctly excluded, and always were — `find -type f` and `rglob` agree here."""
    root = tmp_path / "root"
    (root / "etc/d").mkdir(parents=True)
    (root / "etc/d/real").write_text("x")
    (root / "etc/d/link").symlink_to(root / "etc/d/real")
    conf = tmp_path / "conf"
    conf.write_text("/etc/d\n")

    rels = [rel for _, rel in measured_hashes(root, conf, tdx_measure_script())]
    assert rels == ["/etc/d/real"]


def test_ordering_is_c_collation_not_locale(tmp_path):
    """The boot measurer's `sort` was not pinned to LC_ALL=C, so a locale could
    reorder the extend chain and change RTMR3 with no content change."""
    root = tmp_path / "root"
    (root / "etc").mkdir(parents=True)
    for name in ("Zeta", "alpha", "Beta"):
        (root / "etc" / name).write_text(name)
    conf = tmp_path / "conf"
    conf.write_text("/etc\n")

    rels = [rel for _, rel in measured_hashes(root, conf, tdx_measure_script())]
    # C collation is byte order: uppercase before lowercase. en_US.UTF-8 would interleave.
    assert rels == ["/etc/Beta", "/etc/Zeta", "/etc/alpha"]


def test_backslash_filename_hashes_identically_in_both_paths(tmp_path):
    """The manifest used `sha384sum FILE`, which prefixes its output with '\\'
    for a backslash-containing name (systemd's \\x2d escaping), so the recorded hash could
    never match the clean stdin hash computed at boot — a poweroff on every boot."""
    root = tmp_path / "root"
    (root / "etc").mkdir(parents=True)
    escaped = root / "etc" / "sys-systemd\\x2dcrypt.slice"
    escaped.write_text("unit")
    conf = tmp_path / "conf"
    conf.write_text("/etc\n")

    ((digest, rel),) = measured_hashes(root, conf, tdx_measure_script())
    assert rel == "/etc/sys-systemd\\x2dcrypt.slice"
    assert digest == hashlib.sha384(b"unit").hexdigest()
    assert not digest.startswith("\\")


def test_missing_paths_contribute_nothing(tmp_path):
    root = tmp_path / "root"
    (root / "etc").mkdir(parents=True)
    (root / "etc/present").write_text("x")
    conf = tmp_path / "conf"
    conf.write_text("/etc/present\n/etc/absent\n/opt/nowhere\n")

    rels = [rel for _, rel in measured_hashes(root, conf, tdx_measure_script())]
    assert rels == ["/etc/present"]


def test_nothing_measurable_is_an_error(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    conf = tmp_path / "conf"
    conf.write_text("/etc/absent\n")

    assert _run("hash", root, conf).returncode == 1
    with pytest.raises(Rtmr3Error, match="no files found"):
        measured_hashes(root, conf, tdx_measure_script())


def test_fold_matches_the_hardware_extension_chain(fixture_root):
    """The register the hardware accumulates: rtmr3 = SHA384(rtmr3 || SHA384(contents)),
    from 48 zero bytes, over tdx-measure's per-file hashes in its order."""
    root, conf = fixture_root
    hashes = measured_hashes(root, conf, tdx_measure_script())

    acc = bytes(48)
    for digest, _rel in hashes:
        acc = hashlib.sha384(acc + bytes.fromhex(digest)).digest()
    assert fold_chain(hashes) == acc.hex().upper()


def test_the_guest_gets_the_same_bytes_as_the_bundled_script():
    """The Ansible role installs the guest's /usr/local/bin/tdx-measure by copying THIS
    file out of the checkout. If that ever becomes a second authored copy, the whole
    class of bug above comes back."""
    from pathlib import Path

    repo = Path(__file__).resolve().parents[2]
    role = repo / "ansible/guest/roles/rtmr3-measure/tasks/main.yml"
    task_text = role.read_text()

    assert "src/chutes-cvm/chutes_cvm/scripts/tdx-measure" in task_text
    assert "src/chutes-cvm/chutes_cvm/measurement/rtmr3.py" in task_text
    # And there is exactly one tdx-measure script in the repo.
    found = [
        p for p in repo.rglob("tdx-measure") if p.is_file() and ".git" not in p.parts
    ]
    assert found == [tdx_measure_script()], f"expected one tdx-measure, found {found}"


def test_overlapping_config_entries_measure_each_file_once(tmp_path):
    """A directory and a file beneath it may both be listed; neither is a mistake.

    The config lists directories for coverage and individual files again so they can be
    asserted by name. Without dedupe those files hash twice, which makes the measurement
    depend on the config's redundancy — removing an entry that looks superfluous would
    then change RTMR3 and power off every VM at the mismatch.
    """
    root = tmp_path / "root"
    (root / "usr" / "local" / "bin").mkdir(parents=True)
    (root / "usr" / "local" / "bin" / "cache-rm").write_text("#!/bin/sh\n")
    (root / "usr" / "local" / "bin" / "other").write_text("x\n")

    conf = tmp_path / "conf"
    conf.write_text("/usr/local/bin\n/usr/local/bin/cache-rm\n")

    listed = _run("list", str(root), str(conf)).stdout.splitlines()
    assert listed == sorted(set(listed)), f"duplicate entries in the chain: {listed}"
    assert listed.count("/usr/local/bin/cache-rm") == 1
    assert len(listed) == 2
