"""Tests for the version-level runtime RTMRs (RTMR1/RTMR2/RTMR3).

Covers the pure/host-independent logic — the RTMR3 extension chain and file selection, and
RTMR1/RTMR2 parsing — plus the `build` command's measurements.yaml assembly. The qemu-nbd /
cryptsetup / tdx-measure subprocesses are mocked; the SHA-384 math is real.
"""

import argparse
import hashlib
import json
import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml
from chutes_cvm.measurement import generate_measurements as gm
from chutes_cvm.measurement import runtime_rtmr as rr
from chutes_cvm.measurement.rtmr3 import fold_chain

# ── RTMR3 chain ────────────────────────────────────────────────────────────────


def test_rtmr3_chain_matches_reference(tmp_path):
    """The fold matches an independent reference, and runs the real tdx-measure."""
    root = tmp_path / "root"
    (root / "etc").mkdir(parents=True)
    (root / "etc/a").write_bytes(b"alpha")
    (root / "etc/b").write_bytes(b"beta")
    conf = tmp_path / "conf"
    conf.write_text("/etc/a\n/etc/b\n")

    rtmr3, per_file = rr.rtmr3_chain(str(root), str(conf))

    # Independent reference: rtmr3 = 0x00*48; rtmr3 = SHA384(rtmr3 || SHA384(contents)).
    acc = bytes(48)
    for payload in (b"alpha", b"beta"):
        acc = hashlib.sha384(acc + hashlib.sha384(payload).digest()).digest()
    assert rtmr3 == acc.hex().upper()
    assert [p[1] for p in per_file] == ["/etc/a", "/etc/b"]
    assert per_file[0][0] == hashlib.sha384(b"alpha").hexdigest()


def test_rtmr3_chain_is_order_sensitive():
    """Reordering the same files changes the register — the chain is not a set hash."""
    pairs = [
        (hashlib.sha384(b"x").hexdigest(), "/a"),
        (hashlib.sha384(b"y").hexdigest(), "/b"),
    ]
    assert fold_chain(pairs) != fold_chain(list(reversed(pairs)))


def test_measured_files_sorts_filters_and_strips_comments(tmp_path):
    root = tmp_path / "root"
    (root / "etc/ssh").mkdir(parents=True)
    (root / "etc/ssh/sshd_config").write_text("cfg")
    (root / "etc/hostname").write_text("h")
    (root / "etc/ssh/link").symlink_to(root / "etc/hostname")  # symlink must be skipped
    conf = tmp_path / "conf"
    conf.write_text("/etc/ssh\n/etc/hostname   # inline comment\n# a comment\n\n")

    _, entries = rr.rtmr3_chain(str(root), str(conf))
    rels = [e[1] for e in entries]

    assert rels == sorted(rels)  # sorted by root-relative path
    assert "/etc/hostname" in rels  # inline comment stripped, path still resolved
    assert "/etc/ssh/sshd_config" in rels
    assert "/etc/ssh/link" not in rels  # symlink inside a measured dir filtered out


def test_measured_files_empty_conf_raises(tmp_path):
    conf = tmp_path / "conf"
    conf.write_text("# only comments\n\n")
    with pytest.raises(rr.MeasurementError, match="no paths configured"):
        rr.rtmr3_chain(str(tmp_path), str(conf))


def test_symlinked_conf_entry_is_refused(tmp_path):
    """A conf entry that is itself a symlink used to be followed by the shell walkers
    and skipped by the Python ones; it is now an error on both sides."""
    root = tmp_path / "root"
    (root / "etc").mkdir(parents=True)
    (root / "etc/real").write_text("x")
    (root / "etc/link").symlink_to(root / "etc/real")
    conf = tmp_path / "conf"
    conf.write_text("/etc/real\n/etc/link\n")

    with pytest.raises(rr.MeasurementError, match="symlink"):
        rr.rtmr3_chain(str(root), str(conf))


# ── RTMR1 / RTMR2 ──────────────────────────────────────────────────────────────


def _stage_artifacts(tmp_path):
    base = tmp_path / "img"
    (tmp_path / "img.vmlinuz").write_bytes(b"k")
    (tmp_path / "img.initrd").write_bytes(b"i")
    (tmp_path / "img.cmdline").write_text("console=ttyS0\n")
    return str(base) + ".qcow2"


def test_compute_rtmr1_2_parses_and_uppercases(tmp_path):
    image = _stage_artifacts(tmp_path)
    fake = MagicMock(returncode=0, stdout="RTMR1: abcdef\nRTMR2: 012ABC\n", stderr="")
    with patch("chutes_cvm.measurement.runtime_rtmr.proc.run", return_value=fake):
        r1, r2 = rr.compute_rtmr1_2(image)
    assert r1 == "ABCDEF"
    assert r2 == "012ABC"


def test_compute_rtmr1_2_missing_artifact_raises(tmp_path):
    with pytest.raises(rr.MeasurementError, match="missing direct-boot artifact"):
        rr.compute_rtmr1_2(str(tmp_path / "none.qcow2"))


def test_compute_rtmr1_2_unparseable_output_raises(tmp_path):
    image = _stage_artifacts(tmp_path)
    fake = MagicMock(returncode=0, stdout="nothing useful here\n", stderr="")
    with patch("chutes_cvm.measurement.runtime_rtmr.proc.run", return_value=fake):
        with pytest.raises(rr.MeasurementError, match="could not parse"):
            rr.compute_rtmr1_2(image)


def test_compute_rtmr1_2_metadata_paths_are_absolute(tmp_path, monkeypatch):
    """A relative --image must yield ABSOLUTE kernel/initrd paths in the tdx-measure metadata:
    the fork resolves them relative to the metadata file (a temp dir), not the caller's cwd.
    """
    _stage_artifacts(tmp_path)  # stages img.{vmlinuz,initrd,cmdline} under tmp_path
    monkeypatch.chdir(tmp_path)
    captured = {}

    def _fake_run(cmd, **kwargs):
        # cmd = [tdx-measure, --runtime-only, <meta_path>]; read what got written.
        captured.update(json.loads(Path(cmd[2]).read_text())["direct"])
        return MagicMock(returncode=0, stdout="RTMR1: aa\nRTMR2: bb\n", stderr="")

    with patch("chutes_cvm.measurement.runtime_rtmr.proc.run", side_effect=_fake_run):
        rr.compute_rtmr1_2("img.qcow2")  # relative path

    assert os.path.isabs(captured["kernel"])
    assert captured["kernel"] == str(tmp_path / "img.vmlinuz")
    assert captured["initrd"] == str(tmp_path / "img.initrd")


# ── RTMR3 LUKS handling (always fresh; unlock with LUKS_PASSPHRASE) ─────────────


def _blkid_by_part(types: dict):
    """proc.run side_effect returning the blkid TYPE for the partition in argv."""

    def _run(argv, *a, **k):
        part = argv[-1]  # blkid -o value -s TYPE <part>
        return MagicMock(returncode=0, stdout=types.get(part, "") + "\n", stderr="")

    return _run


def test_detect_root_partition_prefers_luks():
    parts = ["/dev/nbd0p1", "/dev/nbd0p16"]
    types = {"/dev/nbd0p1": "crypto_LUKS", "/dev/nbd0p16": "ext4"}
    with patch(
        "chutes_cvm.measurement.runtime_rtmr.glob.glob", return_value=parts
    ), patch(
        "chutes_cvm.measurement.runtime_rtmr.proc.run",
        side_effect=_blkid_by_part(types),
    ):
        assert rr._detect_root_partition("/dev/nbd0") == ("/dev/nbd0p1", True)


def test_detect_root_partition_plaintext_ext4():
    # No LUKS: the ext4 root is returned, is_luks False (sysfs size read is absent in tests -> 0).
    parts = ["/dev/nbd0p1", "/dev/nbd0p15"]
    types = {"/dev/nbd0p1": "ext4", "/dev/nbd0p15": "vfat"}
    with patch(
        "chutes_cvm.measurement.runtime_rtmr.glob.glob", return_value=parts
    ), patch(
        "chutes_cvm.measurement.runtime_rtmr.proc.run",
        side_effect=_blkid_by_part(types),
    ):
        dev, is_luks = rr._detect_root_partition("/dev/nbd0")
        assert dev == "/dev/nbd0p1"
        assert is_luks is False


def test_detect_root_partition_no_root_raises():
    with patch(
        "chutes_cvm.measurement.runtime_rtmr.glob.glob", return_value=["/dev/nbd0p15"]
    ), patch(
        "chutes_cvm.measurement.runtime_rtmr.proc.run",
        side_effect=_blkid_by_part({"/dev/nbd0p15": "vfat"}),
    ):
        with pytest.raises(rr.MeasurementError, match="no ext4 or LUKS root"):
            rr._detect_root_partition("/dev/nbd0")


def test_compute_rtmr3_luks_without_passphrase_raises(tmp_path):
    # An encrypted root with no LUKS_PASSPHRASE fails closed (never mounts the wrong partition).
    img = tmp_path / "enc.qcow2"
    img.write_bytes(b"x")
    ok = MagicMock(returncode=0, stdout="", stderr="")
    with patch("chutes_cvm.measurement.runtime_rtmr.os.geteuid", return_value=0), patch(
        "chutes_cvm.measurement.runtime_rtmr._have", return_value=True
    ), patch(
        "chutes_cvm.measurement.runtime_rtmr._free_nbd_device", return_value="/dev/nbd0"
    ), patch(
        "chutes_cvm.measurement.runtime_rtmr._wait_for_path", return_value=True
    ), patch(
        "chutes_cvm.measurement.runtime_rtmr._detect_root_partition",
        return_value=("/dev/nbd0p1", True),
    ), patch(
        "chutes_cvm.measurement.runtime_rtmr.proc.run", return_value=ok
    ):
        with pytest.raises(rr.MeasurementError, match="LUKS_PASSPHRASE"):
            rr.compute_rtmr3(str(img))


def test_compute_rtmr3_plaintext_needs_no_cryptsetup(tmp_path):
    # A plaintext (debug) root mounts and measures with cryptsetup absent — same process as prod,
    # minus the luksOpen. Locks in that the debug measurement path is unaffected.
    img = tmp_path / "debug.qcow2"
    img.write_bytes(b"x")
    root = tmp_path / "mnt"
    (root / "etc").mkdir(parents=True)
    (root / "etc/tdx-measure.conf").write_text("/etc/hostname\n")
    (root / "etc/hostname").write_text("h")

    ok = MagicMock(returncode=0, stdout="", stderr="")
    with patch("chutes_cvm.measurement.runtime_rtmr.os.geteuid", return_value=0), patch(
        "chutes_cvm.measurement.runtime_rtmr._have",
        side_effect=lambda t: t != "cryptsetup",
    ), patch(
        "chutes_cvm.measurement.runtime_rtmr._free_nbd_device", return_value="/dev/nbd0"
    ), patch(
        "chutes_cvm.measurement.runtime_rtmr._wait_for_path", return_value=True
    ), patch(
        "chutes_cvm.measurement.runtime_rtmr._detect_root_partition",
        return_value=("/dev/nbd0p1", False),
    ), patch(
        "chutes_cvm.measurement.runtime_rtmr.tempfile.mkdtemp", return_value=str(root)
    ), patch(
        "chutes_cvm.measurement.runtime_rtmr.proc.run", return_value=ok
    ):
        rtmr3, per_file = rr.compute_rtmr3(str(img))

    assert len(rtmr3) == 96  # SHA-384 hex, uppercase
    assert per_file == [(hashlib.sha384(b"h").hexdigest(), "/etc/hostname")]


def test_generate_rtmr3_passes_luks_passphrase_from_env(monkeypatch, capsys):
    monkeypatch.setenv("LUKS_PASSPHRASE", "s3cret")
    args = argparse.Namespace(image="img.qcow2", root_part=None)
    seen = {}

    def _fake(image, root_part=None, luks_passphrase=None):
        seen["passphrase"] = luks_passphrase
        return "COMPUTED", [("hash", "/etc/x")]

    with patch(
        "chutes_cvm.measurement.generate_measurements.compute_rtmr3", side_effect=_fake
    ):
        rc = gm._generate_rtmr3(args)
    assert rc == 0
    assert seen["passphrase"] == "s3cret"
    assert capsys.readouterr().out.strip() == "COMPUTED"


# ── `generate` command → measurements.yaml (compute + write + routing) ──────────


def _gen_args(**over):
    args = dict(
        register=None,
        version="1.4.0",
        image="final.qcow2",
        output="-",
        root_part=None,
        profile="",
        qemu="10.2.1",
        tdx_measure_bin="tdx-measure",
        dist="ubuntu:26.04",
        bios_dir="/fw",
    )
    args.update(over)
    return argparse.Namespace(**args)


def test_compute_measurements_assembles_entry(monkeypatch):
    """The pure compute step returns the teeMeasurements entry (no file I/O)."""
    monkeypatch.setenv("LUKS_PASSPHRASE", "s3cret")
    block = {
        "version": "1.4.0",
        "mrtd": "MRTDHEX",
        "hardware": [{"name": "h", "rtmr0": "R0"}],
    }
    seen = {}

    def _fake_r3(image, root_part=None, luks_passphrase=None):
        seen["passphrase"] = luks_passphrase
        return "R3HEX", [("hash", "/etc/x")]

    with patch.object(gm, "_rtmr0_block", return_value=block), patch(
        "chutes_cvm.measurement.generate_measurements.compute_rtmr1_2",
        return_value=("R1HEX", "R2HEX"),
    ), patch(
        "chutes_cvm.measurement.generate_measurements.compute_rtmr3",
        side_effect=_fake_r3,
    ):
        entry = gm._compute_measurements(_gen_args())

    assert seen["passphrase"] == "s3cret"  # LUKS_PASSPHRASE threaded through to rtmr3
    assert entry["mrtd"] == "MRTDHEX"
    assert entry["rtmr1"] == "R1HEX"
    assert entry["rtmr2"] == "R2HEX"
    assert entry["runtime_rtmr3"] == "R3HEX"
    assert entry["hardware"][0]["rtmr0"] == "R0"
    # Key order matches the chutes-ops values.yaml layout it merges into.
    assert list(entry.keys()) == [
        "version",
        "mrtd",
        "rtmr1",
        "rtmr2",
        "runtime_rtmr3",
        "hardware",
    ]


def test_generate_full_writes_measurements_yaml(tmp_path):
    """A full generate (no --register) serializes the computed entry to --output."""
    out = tmp_path / "measurements.yaml"
    entry = {
        "version": "1.4.0",
        "mrtd": "MRTDHEX",
        "rtmr1": "R1HEX",
        "rtmr2": "R2HEX",
        "runtime_rtmr3": "R3HEX",
        "hardware": [{"name": "h", "rtmr0": "R0"}],
    }
    with patch.object(gm, "_compute_measurements", return_value=entry):
        rc = gm._cmd_generate(_gen_args(output=str(out)))

    assert rc == 0
    doc = yaml.safe_load(out.read_text())
    assert doc["measurements"][0] == entry
    # sort_keys=False preserves the chutes-ops merge layout on disk.
    assert list(doc["measurements"][0].keys()) == [
        "version",
        "mrtd",
        "rtmr1",
        "rtmr2",
        "runtime_rtmr3",
        "hardware",
    ]


def test_generate_full_reports_measurement_error(capsys):
    """A MeasurementError from compute surfaces as exit 1, not a traceback."""
    with patch.object(
        gm, "_compute_measurements", side_effect=rr.MeasurementError("boom")
    ):
        rc = gm._cmd_generate(_gen_args())
    assert rc == 1
    assert "boom" in capsys.readouterr().err


def test_generate_register_rtmr3_routes_and_prints_hex(capsys):
    """`generate --register rtmr3` computes only RTMR3 and prints the bare hex to stdout."""

    def _fake_r3(image, root_part=None, luks_passphrase=None):
        return "R3ONLYHEX", [("hash", "/etc/x")]

    with patch(
        "chutes_cvm.measurement.generate_measurements.compute_rtmr3",
        side_effect=_fake_r3,
    ):
        rc = gm._cmd_generate(_gen_args(register="rtmr3"))
    assert rc == 0
    assert capsys.readouterr().out.strip() == "R3ONLYHEX"


def test_generate_register_rtmr3_without_image_is_usage_error(capsys):
    rc = gm._cmd_generate(_gen_args(register="rtmr3", image=None))
    assert rc == 2
    assert "requires --image" in capsys.readouterr().err


def test_generate_full_without_image_is_usage_error(capsys):
    rc = gm._cmd_generate(_gen_args(image=None))
    assert rc == 2
    assert "--image" in capsys.readouterr().err


# ── API-driven generation: host-profile document → topology, fetch, fingerprint ──

import topology_fixtures as tf  # noqa: E402  (tests/ is on sys.path)

# Minimal discover-profile documents (API `profile` wire shape) for two known classes.
_H200_DOC = {
    "gpu": {
        "pci_device_ids": ["2335"],
        "count": 8,
        "numa_nodes": [0, 0, 0, 0, 1, 1, 1, 1],
    },
    "cpu": {
        "total": 128,
        "sockets": 2,
        "cpu_vendor": "GenuineIntel",
        "cpu_processor_id": "f2060c00fffba91f",
    },
    "memory": {"total_gb": 2048},
    "numa": {"node_count": 2},
    "nvswitch": {"count": 4, "numa_nodes": [0, 0, 0, 0]},
    "launch_determinism": {"qemu_version": "10.2.1"},
}
_RTX_FLAT_DOC = {
    "gpu": {"pci_device_ids": ["2bb5"], "count": 8},
    "cpu": {
        "total": 128,
        "sockets": 2,
        "cpu_vendor": "GenuineIntel",
        "cpu_processor_id": "f3060a00fffba91f",
    },
    "memory": {"total_gb": 2048},
    "numa": {"node_count": 4},  # not 2 → flat fallback
    "launch_determinism": {"qemu_version": "10.2.1"},
}


def test_topology_from_profile_reproduces_numa_fingerprint():
    """The document deriver must reproduce the exact fingerprint the host would launch with —
    here byte-identical to the former hardcoded H200 NVSwitch-node-0 registry entry."""
    profile, fp, qemu = gm.topology_from_profile(_H200_DOC)
    assert profile.display_name == "8xh200"
    assert qemu == "10.2.1"
    assert fp == tf.H200_KR6288  # vcpus 124, mem 1128, NUMA gpu 4+4, nvsw node 0


def test_topology_from_profile_flat_fallback():
    profile, fp, qemu = gm.topology_from_profile(_RTX_FLAT_DOC)
    assert profile.display_name == "8xpro_6000"
    assert fp == tf.RTX_FLAT  # >2 NUMA nodes → FlatTopology(gpu_count=8), mem 768


def test_topology_from_profile_rejects_unknown_device():
    with pytest.raises(ValueError, match="no GPU profile matches"):
        gm.topology_from_profile({"gpu": {"pci_device_ids": ["dead"], "count": 8}})


class _Resp:
    def __init__(self, payload):
        self._payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return json.dumps(self._payload).encode()


def _fetch_capturing_url(include_pending):
    payload = [{"fingerprint": "a" * 64, "measured": True, "profile": _H200_DOC}]
    seen = {}

    def _urlopen(req, *a, **k):
        seen["url"] = req.full_url
        return _Resp(payload)

    with patch(
        "chutes_cvm.measurement.generate_measurements.urllib.request.urlopen",
        side_effect=_urlopen,
    ):
        out = gm.fetch_host_profiles(
            "https://api.example", include_pending=include_pending
        )
    return out, seen["url"]


def test_fetch_host_profiles_measured_only_by_default():
    out, url = _fetch_capturing_url(include_pending=False)
    assert out[0]["fingerprint"] == "a" * 64
    assert url.endswith("/servers/tdx/host_profiles")
    assert "include_pending" not in url


def test_fetch_host_profiles_include_pending_adds_query():
    _, url = _fetch_capturing_url(include_pending=True)
    assert url.endswith("/servers/tdx/host_profiles?include_pending=true")


def _rtmr0_args(**over):
    d = dict(
        api_base="https://api.example",
        include_pending=False,
        bios_dir="/fw",
        tdx_measure_bin="tdx-measure",
        dist="ubuntu:26.04",
        version="1.4.0",
    )
    d.update(over)
    return argparse.Namespace(**d)


def test_rtmr0_block_carries_api_fingerprint_onto_each_entry():
    """The API's fingerprint is stamped onto the generated entry (never recomputed), so the
    reconciler can join the published measurement to the submitted host profile."""
    fp_hex = "b" * 64
    records = [{"fingerprint": fp_hex, "profile": _H200_DOC}]
    with patch.object(gm, "fetch_host_profiles", return_value=records), patch.object(
        gm, "generate_acpi_blobs", return_value={"rtmr0": "R0HEX", "mrtd": "MRTDHEX"}
    ):
        block = gm._rtmr0_block(_rtmr0_args())
    assert len(block["hardware"]) == 1
    entry = block["hardware"][0]
    assert entry["fingerprint"] == fp_hex
    assert entry["rtmr0"] == "R0HEX"
    assert block["mrtd"] == "MRTDHEX"
    assert "8xh200" in entry["name"]


def test_rtmr0_block_marks_unfingerprinted_record_pending():
    records = [{"fingerprint": "", "profile": _H200_DOC}]
    with patch.object(gm, "fetch_host_profiles", return_value=records), patch.object(
        gm, "generate_acpi_blobs", return_value={"rtmr0": "R0", "mrtd": "M"}
    ):
        block = gm._rtmr0_block(_rtmr0_args())
    assert block["hardware"] == []
    assert block.get("pending_profiles")
