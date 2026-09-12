#!/usr/bin/env python3
"""TDX measurements generator → the version's teeMeasurements block.

The known host classes come from the API (the source of truth): `generate` reads the published
host profiles (`GET /servers/tdx/host_profiles`) and produces one entry per class, carrying the
API's fingerprint through onto it so the reconciler can join the published measurement to the
submitted host profile. RTMR generation itself is offline (fork + Docker; no TDX/GPU).

By default only *measured* host classes are processed — the set a third party can verify against
an already-published measurement. `--include-pending` also processes classes awaiting generation
(the generator's queue: a newly submitted profile is "pending" until its measurement exists), which
is what the release build passes to turn new submissions into published measurements.

`generate` (no --register) computes the whole block for an image version — MRTD +
per-topology RTMR0 + RTMR1/RTMR2 (from the staged direct-boot artifacts) + RTMR3
(over the encrypted root's /etc/tdx-measure.conf files) — and writes measurements.yaml.
`--register {rtmr0,rtmr3}` narrows it to one register (standalone partials for the
GPU-VM build, which has no aggregation). `list` prints the API's known host classes.

The bulk of this module is the novel part — offline per-topology RTMR0 generation (no
guest boot), from local/offline-rtmr0-findings.md §7:
RTMR0 is a SHA-384 chain over the CCEL's MrIndex==1 records — 14 events on this
branch's direct boot (19 on indirect, with the #15-18 boot variables) — of which
**5 vary** per topology and the rest are constant (firmware/boot). From one baseline
CCEL (the constants) plus a per-topology recompute of the varying events, splice +
replay → rtmr0. The varying events are located by identity (locate_rtmr0_events), so
the splice is correct regardless of that boot-method count.

The 5 varying events and how each is reproduced offline (no guest boot):

    #0   TdxTable (TD-HOB)      measure_td_hob(memory)      — per (mem) class
    #11  SHA384(etc/table-loader)   SHA384(fw_cfg blob)     — per topology
    #12  SHA384(etc/acpi/rsdp)      SHA384(fw_cfg blob)     — per topology
    #13  SHA384(etc/acpi/tables)    SHA384(fw_cfg blob)     — per topology
    #14  SMBIOS handoff         from baseline — host/topology-invariant (pinned identity)

#0 and #11-13 are recomputed per topology by the fork; #14 and the constants come from the
one baseline CCEL. SMBIOS's only host-varying input (the type-1/2/3 identity) is pinned this
release, so #14 does not vary by host or topology — one CCEL, captured anywhere, generates
every profile. (Recomputing #14 offline from the SMBIOS blob, to drop the CCEL entirely, is
future work — see utils/smbios_match.py.)

Needs network access to the API for the host-profile list, and — for actual per-topology ACPI
generation — the chutesai/tdx-measure fork + Docker on any x86-64 Linux (NO TDX, NO GPUs —
that's the point of offline measurement). The splice/replay/recompute/assembly path is pure
stdlib.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

import yaml
from chutes_cvm import proc
from chutes_cvm.guest.gpu.profiles import GPU_PROFILES, GpuProfile
from chutes_cvm.guest.gpu.topology import (
    CpuTopology,
    FlatTopology,
    NumaTopology,
    TopologyFingerprint,
)
from chutes_cvm.measurement import ccel_replay as cc
from chutes_cvm.measurement.platform_tables import MeasurementMetadata
from chutes_cvm.measurement.runtime_rtmr import (
    MeasurementError,
    compute_rtmr1_2,
    compute_rtmr3,
)
from chutes_cvm.measurement.topology_spec import (
    build_topology_spec,
    measurement_cpu_args,
)
from chutes_cvm.paths import firmware_dir

# The API is the source of truth for known host classes and their fingerprints. `generate`
# reads the published host profiles (the platform inputs each measurement is built from) from
# this public, unauthenticated endpoint and generates one measurement per profile, carrying the
# API's fingerprint straight through — the reconciler joins published measurements to submitted
# host profiles on it, so an entry without a fingerprint is unmatchable.
DEFAULT_API_BASE = "https://api.chutes.ai"
_HOST_PROFILES_PATH = "/servers/tdx/host_profiles"

# The topology-varying RTMR0 events are located BY IDENTITY (event type + descriptor),
# not by fixed position: the boot method sets how many CONSTANT events surround them
# (indirect boot = 19 events total; direct boot = 14 — no #15-18 boot variables and one
# fewer QEMU FW CFG), which shifts absolute positions. We recompute #0 (TD-HOB) and the
# three ACPI DATA digests; #14 (SMBIOS) and every constant stay from the baseline.
_EV_HANDOFF_TABLES2 = 0x8000000B  # TD-HOB (#0); data contains "TdxTable"
_EV_PLATFORM_CONFIG_FLAGS = (
    0x0000000A  # the three ACPI DATA events (#11-13) share this type
)

# The three ACPI DATA events, in fold order, → their fw_cfg blob name (as staged by
# extract-measurements.sh) for the #11-13 recompute.
ACPI_BLOB_FILES = ["table_loader.bin", "rsdp.bin", "acpi_tables.bin"]

# The fork's rtmr0_log (tdx-measure --json-file) indices for the events it recomputes:
# [0] = TD-HOB, [8,9,10] = the three ACPI DATA digests (table-loader / rsdp / acpi-tables).
FORK_TDHOB_IDX = 0
FORK_ACPI_IDX = (8, 9, 10)


def overrides_from_fork_log(
    rtmr0_log: list[str], tdhob_idx: int, acpi_idx: list[int]
) -> dict[int, bytes]:
    """Map the fork's recomputed digests onto the located baseline indices (from
    locate_rtmr0_events): fork #0 → TD-HOB, fork [8,9,10] → the three ACPI DATA events,
    in order. #14 (SMBIOS, same (mem,cpu) class) and #2/3/4 stay from the baseline."""
    need = max((FORK_TDHOB_IDX, *FORK_ACPI_IDX)) + 1
    if len(rtmr0_log) < need:
        raise ValueError(
            f"fork rtmr0_log has {len(rtmr0_log)} events, need >= {need}. "
            "Rebuild the tdx-measure fork with the rtmr0_log field, and confirm "
            "direct-boot mode (kernel/initrd set in the metadata)."
        )
    out = {tdhob_idx: bytes.fromhex(rtmr0_log[FORK_TDHOB_IDX])}
    for baseline_i, fork_i in zip(acpi_idx, FORK_ACPI_IDX):
        out[baseline_i] = bytes.fromhex(rtmr0_log[fork_i])
    return out


# ── Core: splice + replay ─────────────────────────────────────────────────────


def mr1_events(events: list[cc.Event]) -> list[cc.Event]:
    """The MrIndex==1 events that fold into RTMR0 (skipping EV_NO_ACTION), in order.
    Its length is boot-method-dependent (direct=14, indirect=19), so index the events
    the splice touches via locate_rtmr0_events, not by a fixed findings-§1 number."""
    return [e for e in events if e.mr_index == 1 and e.event_type != cc.EV_NO_ACTION]


def locate_rtmr0_events(events: list[cc.Event]) -> tuple[int, list[int]]:
    """Locate the spliced events in the MrIndex==1 fold BY IDENTITY, so it works for any
    boot method's event count. Returns (tdhob_index, [three acpi indices]): the TD-HOB
    (EV_EFI_HANDOFF_TABLES2 / "TdxTable") and the three ACPI DATA events
    (EV_PLATFORM_CONFIG_FLAGS / "ACPI DATA"), in fold order."""
    tdhob = None
    acpi: list[int] = []
    for i, e in enumerate(mr1_events(events)):
        if e.event_type == _EV_HANDOFF_TABLES2 and b"TdxTable" in e.data:
            tdhob = i
        elif e.event_type == _EV_PLATFORM_CONFIG_FLAGS and b"ACPI DATA" in e.data:
            acpi.append(i)
    if tdhob is None or len(acpi) != 3:
        raise cc.EventLogError(
            f"unexpected RTMR0 layout: TD-HOB found={tdhob is not None}, "
            f"{len(acpi)} ACPI DATA events (need exactly 1 + 3)."
        )
    return tdhob, acpi


def replay_with_overrides(
    events: list[cc.Event], overrides: dict[int, bytes], alg: int = cc.RTMR_ALG
) -> bytes:
    """Fold RTMR0 (MrIndex==1) substituting overrides[i] (i = index into mr1_events) for
    that event's SHA-384 digest. This is the splice: constant events keep their baseline
    digest, the located varying events get their recomputed one."""
    hash_name = cc._ALG_NAMES[alg]
    acc = b"\x00" * cc._ALG_SIZES[alg]
    for pos, ev in enumerate(mr1_events(events)):
        digest = overrides.get(pos)
        if digest is None:
            digest = ev.digest(alg)
            if digest is None:
                raise cc.EventLogError(f"event #{pos} ({ev.type_name}) missing digest")
        acc = hashlib.new(hash_name, acc + digest).digest()
    return acc


def acpi_digests(acpi_dir: str | Path, acpi_idx: list[int]) -> dict[int, bytes]:
    """{baseline_index: SHA384(blob)} for the three ACPI DATA events, mapping each located
    index to its fw_cfg blob (table-loader / rsdp / acpi-tables, in fold order). This is the
    #11-13 recompute, proven in findings §2: the ACPI DATA digests are literally SHA384 of
    the bytes."""
    acpi_dir = Path(acpi_dir)
    out: dict[int, bytes] = {}
    for baseline_i, fname in zip(acpi_idx, ACPI_BLOB_FILES):
        blob = acpi_dir / fname
        if not blob.exists():
            raise FileNotFoundError(f"missing fw_cfg blob {fname}: {blob}")
        out[baseline_i] = hashlib.sha384(blob.read_bytes()).digest()
    return out


# ── Per-topology ACPI generation (build host: tdx-measure + Docker) ────────────


def generate_acpi_blobs(
    metadata: dict, out_dir: Path, *, tdx_measure_bin: str, dist: str
) -> dict:
    """Run `tdx-measure --create-acpi-tables` to dump the topology's fw_cfg ACPI
    blobs and its {mrtd, rtmr0}. Mirrors local/scripts/run_validation.sh:43. The
    generated etc/acpi/* land next to `acpi_tables` in the metadata; we read those
    for the #11-13 recompute. Returns the parsed tdx-measure JSON ({mrtd, rtmr0}).

    Runs OFFLINE on any x86-64 Linux with Docker + the fork — no TDX, no GPUs. KVM
    speeds the brief ACPI-gen QEMU run but isn't required; reserve=off (applied by
    platform_tables.MeasurementMetadata) lifts the guest-sized-RAM requirement.

    Only the distribution is passed to --create-acpi-tables: the fork pins the exact
    QEMU source-package version *and* container image digest per dist (qemu_pkg_for),
    which is what makes the dump reproducible. The QEMU version label (e.g. "10.2.1", from the
    host profile) is a release label, NOT a Debian package version — forwarding it as the fork's
    version override lands an unresolvable `pull-lp-source qemu 10.2.1`.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    meta_path = out_dir / "metadata.json"
    result_path = out_dir / "result.json"
    meta_path.write_text(json.dumps(metadata, indent=2))
    # The metadata positional is placed first: --create-acpi-tables is num_args=1..=2,
    # so a metadata path immediately after `dist` would be greedily eaten as the version.
    # Capture the output (the fork's docker build log is very noisy) and, on failure,
    # raise just the tail — the caller renders it as a one-line PENDING reason.
    result = proc.run(
        [
            tdx_measure_bin,
            str(meta_path),
            "--platform-only",
            "--json-file",
            str(result_path),
            "--create-acpi-tables",
            dist,
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        tail = "\n    ".join(
            (result.stderr or result.stdout or "").strip().splitlines()[-4:]
        )
        raise RuntimeError(
            f"tdx-measure --create-acpi-tables (dist={dist}) failed "
            f"(exit {result.returncode}):\n    {tail}"
        )
    return json.loads(result_path.read_text())


# ── Host profiles (from the API) → per-topology generation inputs ──────────────


def fetch_host_profiles(api_base: str, include_pending: bool = False) -> list[dict]:
    """GET the published host profiles: ``[{"fingerprint", "measured", "profile"}, ...]``.

    The API owns the fingerprint and is the source of truth for known host classes. This public,
    unauthenticated endpoint returns each stored discover-profile document plus its 64-hex
    fingerprint; the generator builds one measurement per profile and carries the fingerprint
    through verbatim (never recomputed).

    Default is measured-only — the host classes a third party can verify against a published
    measurement. ``include_pending`` also returns classes awaiting generation (the generator's
    queue: a submitted profile is only "measured" once its measurement exists, so generation must
    fetch the pending set first)."""
    url = f"{api_base.rstrip('/')}{_HOST_PROFILES_PATH}"
    if include_pending:
        url += "?include_pending=true"
    req = urllib.request.Request(
        url, headers={"User-Agent": "chutes-cvm-measurements/1.0"}
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:  # nosec B310
            data = json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        raise ValueError(f"API returned HTTP {exc.code} for {url}") from exc
    except urllib.error.URLError as exc:
        raise ValueError(f"API unreachable at {url}: {exc.reason}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"API returned unparseable host profiles: {exc}") from exc
    if not isinstance(data, list):
        raise ValueError(
            f"expected a list of host profiles from {url}, got {type(data).__name__}"
        )
    return data


def _resolve_profile_for_devices(device_ids: list[str]) -> GpuProfile:
    """The GpuProfile for these GPUs — the measurement policy (firmware, CC/PPCIe mode,
    BAR/VRAM, reserved CPUs, guest-RAM rule) for the class.

    All GPUs must be one model. This used to return the first profile any single id
    matched, so insertion order decided the answer rather than the evidence and a
    heterogeneous host was measured as whichever model sorted first. The launch path has
    always refused to guess (``guest.gpu.profiles.resolve_profile``), and the caller here
    treats a raise as PENDING, so a malformed document is skipped visibly rather than
    measured plausibly.

    One id per profile, so comparing ids IS comparing profiles.
    """
    unique = set(device_ids)
    if len(unique) != 1:
        raise ValueError(
            f"all GPUs must be one model; device ids are {sorted(unique)}"
            if unique
            else "host profile reports no GPU device ids"
        )
    device_id = unique.pop()
    for profile in GPU_PROFILES.values():
        if profile.matches_device_id(device_id):
            return profile
    raise ValueError(f"no GPU profile matches device id {device_id}")


def topology_from_profile(doc: dict) -> "tuple[GpuProfile, TopologyFingerprint, str]":
    """Derive ``(GpuProfile, TopologyFingerprint, qemu_version)`` from an API host-profile document.

    ``doc`` is discover-profile.sh's output as stored by the API. This mirrors the live
    ``host_topology_fingerprint`` but reads the document instead of sysfs, so the generator
    reproduces the exact RTMR0 inputs the host launches with. The fingerprint that identifies the
    class is the API's (carried separately) — it is never recomputed here.
    """
    gpu = doc.get("gpu") or {}
    cpu = doc.get("cpu") or {}
    memory = doc.get("memory") or {}
    numa = doc.get("numa") or {}
    nvswitch = doc.get("nvswitch") or {}
    nic = doc.get("nic") or {}
    qemu = (doc.get("launch_determinism") or {}).get("qemu_version") or ""

    device_ids = [str(d).lower() for d in (gpu.get("pci_device_ids") or [])]
    profile = _resolve_profile_for_devices(device_ids)
    gpu_count = int(gpu.get("count") or 0)

    cpu_topo = CpuTopology(
        vcpus=int(cpu.get("total") or 0) - profile.host_reserved_cpus,
        sockets=int(cpu.get("sockets") or 0),
        cpu_vendor=cpu.get("cpu_vendor") or "",
        cpu_processor_id=cpu.get("cpu_processor_id"),
    )
    mem_gb = profile.guest_mem_gb(int(memory.get("total_gb") or 0), gpu_count)

    gpu_topo: "NumaTopology | FlatTopology"
    if profile.enable_numa_topology and int(numa.get("node_count") or 0) == 2:
        gpu_topo = NumaTopology(
            gpu_nodes=tuple(gpu.get("numa_nodes") or ()),
            nvswitch_nodes=tuple(nvswitch.get("numa_nodes") or ()),
            ib_nodes=tuple(nic.get("passthrough_numa_nodes") or ()),
        )
    else:
        gpu_topo = FlatTopology(
            gpu_count=gpu_count,
            nvswitch_count=int(nvswitch.get("count") or 0),
            ib_count=int(nic.get("ib_class_count") or 0),
        )
    return profile, TopologyFingerprint(cpu_topo, mem_gb, gpu_topo), qemu


# ── CLI ───────────────────────────────────────────────────────────────────────


def _rtmr0_block(args: argparse.Namespace) -> dict:
    """Generate the version-level RTMR0 block: {version, mrtd, hardware[], pending_profiles?}.

    Reads the published host profiles from the API (the source of truth for known host classes)
    and generates ONE hardware entry per profile. For each, the fork self-generates the COMPLETE
    RTMR0 (all 15 events, no CCEL) from the topology derived off the profile document, and the
    API's fingerprint is carried through onto the entry (never recomputed) so the reconciler can
    join it to the submitted host profile. A profile that can't be generated offline yet (e.g. an
    uncaptured CPU model — cpu_processor_id null) is listed PENDING by fingerprint, not fatal.

    Raises ValueError on a hard error (API unreachable, duplicate hardware names, or MRTD
    divergence across topologies). Needs the fork + Docker (offline, any x86-64 Linux).
    """

    def fork_rtmr0(profile, fp, qemu):
        spec = build_topology_spec(
            profile,
            fp,
            cpu_args=measurement_cpu_args(fp, qemu),
            firmware=str(Path(args.bios_dir) / profile.firmware_filename),
        )
        with tempfile.TemporaryDirectory() as td:
            meta = MeasurementMetadata(
                spec, profile, fp, acpi_tables=str(Path(td) / "acpi.bin")
            ).to_dict()
            out = generate_acpi_blobs(
                meta,
                Path(td),
                tdx_measure_bin=args.tdx_measure_bin,
                dist=args.dist,
            )
        return (out.get("rtmr0") or "").upper(), out.get("mrtd", "")

    records = fetch_host_profiles(args.api_base, args.include_pending)
    hardware: list[dict] = []  # flat teeMeasurements `hardware` entries
    mrtds: set[str] = set()
    pending: list[str] = []
    for record in records:
        fingerprint = record.get("fingerprint") or ""
        label = fingerprint[:12] or "<no-fingerprint>"
        try:
            if not fingerprint:
                raise ValueError("host profile has no fingerprint")
            profile, fp, qemu = topology_from_profile(record.get("profile") or {})
            rtmr0, mrtd = fork_rtmr0(profile, fp, qemu)
            mrtds.add(mrtd.upper())
            gpu_count = getattr(fp.gpu, "gpu_count", None) or len(
                getattr(fp.gpu, "gpu_nodes", ())
            )
            hw_name = f"{profile.display_name} [{qemu}, {fp.variant_label}]"
            hardware.append(
                {
                    "name": hw_name,
                    "description": (
                        f"{gpu_count}x {profile.expected_gpus[0].upper()} "
                        "GPU configuration"
                    ),
                    "fingerprint": fingerprint,
                    "rtmr0": rtmr0,
                    "expected_gpus": list(profile.expected_gpus),
                    "gpu_count": gpu_count,
                }
            )
            print(f"    {hw_name}  fp={label}…  rtmr0={rtmr0[:16]}…", file=sys.stderr)
        except Exception as exc:
            pending.append(fingerprint or label)
            print(
                f"  {label}: PENDING — cannot generate offline: {exc}", file=sys.stderr
            )
            continue

    # Every hardware entry must have a globally-unique name — the computed
    # display_name + variant_label guarantee this today; assert it so a future
    # profile/topology collision fails the build loudly instead of silently merging.
    counts: dict[str, int] = {}
    for e in hardware:
        counts[e["name"]] = counts.get(e["name"], 0) + 1
    dupes = sorted(n for n, c in counts.items() if c > 1)
    if dupes:
        raise ValueError(f"duplicate hardware names: {dupes}")
    # MRTD is version-level (same OVMF/TDVF across every topology of a build).
    if len(mrtds) > 1:
        raise ValueError(f"MRTD differs across topologies: {sorted(mrtds)}")

    block: dict = {
        "version": args.version,
        "mrtd": next(iter(mrtds), ""),
        "hardware": hardware,
    }
    if pending:
        block["pending_profiles"] = sorted(set(pending))
    return block


def _write_output(payload: str, output: str) -> None:
    """Write ``payload`` to ``output`` — ``-`` = stdout; otherwise mkdir -p the parent, write
    the file, and note it on stderr. Shared by the register-generating subcommands."""
    if output == "-":
        sys.stdout.write(payload)
        return
    out = Path(output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(payload)
    print(f"wrote {out}", file=sys.stderr)


def _generate_rtmr0(args: argparse.Namespace) -> int:
    """`generate --register rtmr0`: compute just the RTMR0 block (version-level mrtd + per-topology
    hardware list) and write it as JSON to --output. A standalone/debug partial — a full `generate`
    computes RTMR0 inline (via _compute_measurements), so this is no longer an input to it.
    """
    try:
        block = _rtmr0_block(args)
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    _write_output(json.dumps(block, indent=2) + "\n", args.output)
    pending = block.get("pending_profiles")
    n = len(block["hardware"])
    print(
        f"{n} hardware entr{'y' if n == 1 else 'ies'} generated"
        f"{f', pending: {pending}' if pending else ''}",
        file=sys.stderr,
    )
    # Fail if the API returned host classes but none could be generated (all pending).
    return 2 if (block.get("pending_profiles") and not block["hardware"]) else 0


def _compute_measurements(args: argparse.Namespace) -> dict:
    """Compute EVERY register for a version and return the assembled teeMeasurements entry.

    POST-LUKS, from the finalized image: version-level mrtd + per-topology rtmr0 (the fork),
    rtmr1/rtmr2 (the image's staged direct-boot artifacts), and rtmr3 (mounting the root —
    unlocking it with the LUKS_PASSPHRASE env var when the image is already encrypted). Pure
    data assembly — no file output; raises ValueError (topology/aggregation) or MeasurementError
    (rtmr1/2/3) on failure. Replaces the old compute-rtmr0/1-2/rtmr3 + aggregate roles.
    """
    block = _rtmr0_block(args)
    rtmr1, rtmr2 = compute_rtmr1_2(args.image, tdx_measure_bin=args.tdx_measure_bin)
    rtmr3, _ = compute_rtmr3(
        args.image, luks_passphrase=os.environ.get("LUKS_PASSPHRASE")
    )
    print(
        f"    RTMR1={rtmr1[:16]}…  RTMR2={rtmr2[:16]}…  RTMR3={rtmr3[:16]}…",
        file=sys.stderr,
    )
    pending = block.get("pending_profiles")
    if pending:
        print(f"    pending profiles: {pending}", file=sys.stderr)

    # Insertion order (version → mrtd → rtmr1/2 → runtime_rtmr3 → hardware) matches the
    # chutes-ops values.yaml teeMeasurements layout this merges into; sort_keys=False keeps it.
    return {
        "version": args.version,
        "mrtd": block["mrtd"],
        "rtmr1": rtmr1,
        "rtmr2": rtmr2,
        "runtime_rtmr3": rtmr3,
        "hardware": block["hardware"],
    }


def _generate_full(args: argparse.Namespace) -> int:
    """A full `generate` (no --register): compute every register (via _compute_measurements) and
    write the version's single measurements.yaml to --output. compute → serialize → write.
    """
    try:
        entry = _compute_measurements(args)
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    except MeasurementError as exc:
        print(f"ERROR (rtmr1/2/3): {exc}", file=sys.stderr)
        return 1

    payload = yaml.safe_dump(
        {"measurements": [entry]}, sort_keys=False, indent=2, default_flow_style=False
    )
    _write_output(payload, args.output)
    n = len(entry["hardware"])
    print(
        f"measurements.yaml: {n} hardware entr{'y' if n == 1 else 'ies'}",
        file=sys.stderr,
    )
    return 0


def _generate_rtmr3(args: argparse.Namespace) -> int:
    """`generate --register rtmr3`: compute the version-level RTMR3 fresh from the image. Prints
    the bare hex to stdout (per-file hashes to stderr) for a caller to capture as a fact.

    Mounts the root read-only. If it is already LUKS-encrypted, set the LUKS_PASSPHRASE env var
    (the passphrase the image was encrypted with) to unlock it and recompute — always a fresh
    value, never a cached one.
    """
    try:
        rtmr3, per_file = compute_rtmr3(
            args.image,
            root_part=args.root_part,
            luks_passphrase=os.environ.get("LUKS_PASSPHRASE"),
        )
    except MeasurementError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    print(f"  Measuring {len(per_file)} files:", file=sys.stderr)
    for file_hash, rel in per_file:
        print(f"  {file_hash}  {rel}", file=sys.stderr)
    print(f"RTMR3: {rtmr3}", file=sys.stderr)
    print(rtmr3)  # bare hex to stdout
    return 0


def _usage_error(msg: str) -> int:
    """Print an argparse-style usage error to stderr and return exit code 2."""
    print(f"chutes-cvm measurements generate: {msg}", file=sys.stderr)
    return 2


def _cmd_generate(args: argparse.Namespace) -> int:
    """Route `measurements generate` by --register: none = the full measurements.yaml (every
    register); rtmr0 = just the RTMR0 JSON block; rtmr3 = just the bare RTMR3 hex. Each mode
    needs different inputs, validated here (argparse can't require them conditionally).
    """
    if args.register == "rtmr0":
        if not args.version:
            return _usage_error("--register rtmr0 requires --version")
        return _generate_rtmr0(args)
    if args.register == "rtmr3":
        if not args.image:
            return _usage_error("--register rtmr3 requires --image")
        return _generate_rtmr3(args)
    missing = [
        flag
        for flag, val in (("--version", args.version), ("--image", args.image))
        if not val
    ]
    if missing:
        return _usage_error(f"a full generate requires {' and '.join(missing)}")
    return _generate_full(args)


def _cmd_list(args: argparse.Namespace) -> int:
    """List the published host classes the API knows — the profiles `generate` builds
    measurements for. Prints ``<fingerprint> [pending] <count>x [<pci_device_ids>]`` per class.
    """
    for record in fetch_host_profiles(args.api_base, args.include_pending):
        fp = record.get("fingerprint") or "<no-fingerprint>"
        state = "" if record.get("measured", True) else " [pending]"
        gpu = (record.get("profile") or {}).get("gpu") or {}
        ids = ",".join(gpu.get("pci_device_ids") or []) or "?"
        print(f"{fp}{state}  {gpu.get('count', '?')}x [{ids}]")
    return 0


def _add_api_arg(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "--api-base",
        default=os.environ.get("CHUTES_API_BASE") or DEFAULT_API_BASE,
        help="control-plane base URL for the host-profile source "
        f"(default: {DEFAULT_API_BASE}; env CHUTES_API_BASE)",
    )
    p.add_argument(
        "--include-pending",
        action="store_true",
        help="also process host classes awaiting measurement generation (the generator's "
        "queue — use after a new host profile is submitted); default is measured classes only, "
        "which is what third parties verify against published measurements",
    )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="chutes-cvm measurements",
        description=__doc__.splitlines()[0],
    )
    sub = ap.add_subparsers(dest="cmd", required=True)

    ls = sub.add_parser(
        "list", help="list the API's known host classes (fingerprint + GPUs)"
    )
    _add_api_arg(ls)
    ls.set_defaults(func=_cmd_list)

    def _add_fork_args(p: argparse.ArgumentParser) -> None:
        """Shared RTMR0-generation options (the tdx-measure fork inputs + host-profile source)."""
        _add_api_arg(p)
        p.add_argument(
            "--tdx-measure-bin",
            default="tdx-measure",
            help="path to the tdx-measure fork binary",
        )
        p.add_argument(
            "--dist", default="ubuntu:26.04", help="ACPI-dump container base image"
        )
        p.add_argument(
            "--bios-dir",
            default=str(firmware_dir()),
            help="directory holding the OVMF firmware (profile.firmware_filename); the fork "
            "opens the metadata's 'bios' path, so it must resolve absolutely "
            "(default: chutes-cvm firmware dir; env CHUTES_CVM_FIRMWARE_DIR)",
        )

    gen = sub.add_parser(
        "generate",
        help="generate the version's measurements — by default EVERY register into "
        "measurements.yaml; --register narrows it to one (build host: fork + Docker/KVM; "
        "LUKS_PASSPHRASE unlocks an encrypted root for RTMR3)",
        description="Generate TDX measurements for an image version. With no --register this "
        "computes the complete set — mrtd + rtmr0 (all topologies) + rtmr1/rtmr2 + rtmr3 — from "
        "the finalized (post-LUKS) image and writes measurements.yaml. --register restricts it to "
        "a single register (a standalone partial): rtmr0 emits the mrtd+rtmr0 JSON block; rtmr3 "
        "emits the bare RTMR3 hex to stdout.",
    )
    _add_fork_args(gen)
    gen.add_argument(
        "--register",
        choices=("rtmr0", "rtmr3"),
        default=None,
        help="generate only this register instead of the full set (rtmr0 = mrtd+rtmr0 JSON; "
        "rtmr3 = bare hex). Omit for the complete measurements.yaml.",
    )
    gen.add_argument(
        "--version",
        default=None,
        help="image version (required for the full set and --register rtmr0)",
    )
    gen.add_argument(
        "--image",
        default=None,
        help="finalized (post-luks) qcow2 (required for the full set and --register rtmr3): its "
        "staged .vmlinuz/.initrd/.cmdline pin RTMR1/RTMR2; its root (unlocked via LUKS_PASSPHRASE "
        "if encrypted) yields RTMR3",
    )
    gen.add_argument(
        "--output",
        default="-",
        help="output path (measurements.yaml for the full set, JSON for --register rtmr0); "
        "'-' = stdout (default). --register rtmr3 always prints its hex to stdout.",
    )
    gen.add_argument(
        "--root-part",
        default=None,
        help="root partition override for --register rtmr3: an absolute /dev path or an nbd "
        "suffix like 'p1' (default: auto-detect the LUKS/ext4 root on the nbd device)",
    )
    gen.set_defaults(func=_cmd_generate)

    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
