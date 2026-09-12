"""Attestation preflight — ask the control plane whether this exact image can launch here.

The miner never computes or matches a topology fingerprint: it captures its raw platform
metadata (``discover-profile.sh``), signs it with the miner hotkey, and POSTs it to
``api.chutes.ai``. Three of the four host-profile operations live here:

    run_host_class_status()     — POST /servers/tdx/host_profiles/status: is this topology known,
                                  and which published images cover it? Version-free, so it answers
                                  on a host that has downloaded nothing yet -> `host verify`
    run_preflight(version, rc)  — POST /servers/tdx/preflight: does a published measurement for
                                  THIS image's (version, rc) cover this host? -> ``launchable`` bool
    submit_profile()            — POST /servers/tdx/host_profiles: register an unmeasured class so
                                  Chutes generates its measurements

(The fourth, GET /servers/tdx/host_profiles, is the generator's/third-party listing — not here.)

The split is the two questions a miner actually asks: `host verify` asks about the HOST ("can this
box run anything, and what"), which must answer before any image is downloaded; `guest launch` asks
about the IMAGE ("is the version I hold covered here") and gets one boolean. The API owns the
fingerprint and both verdicts; if that key ever changes it changes there, not here.
"""

from __future__ import annotations

import hashlib
import json
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlencode

import yaml
from chutes_cvm import proc
from chutes_cvm.guest.detection import GUEST_CPU_ARGS, SUPPORTED_QEMU_BY_OS
from chutes_cvm.paths import DEFAULT_API_BASE
from substrateinterface import Keypair, KeypairType

# A transport/auth failure (no verdict) fails CLOSED — the boot's LUKS key release needs the API
# anyway, so refusing to launch when we cannot confirm loses nothing.
FAIL_CLOSED = 1


class PreflightError(Exception):
    """Any failure that prevents getting a verdict (bad config, transport, API error)."""


def _load_miner_creds(config_path: str) -> "tuple[str, str]":
    """(ss58, seed) from the launch config.yaml's ``miner`` block."""
    try:
        with open(config_path) as f:
            cfg = yaml.safe_load(f) or {}
    except OSError as exc:
        raise PreflightError(f"cannot read config {config_path}: {exc}") from exc
    miner = cfg.get("miner") or {}
    ss58 = str(miner.get("ss58") or "").strip()
    seed = str(miner.get("seed") or "").strip()
    if not ss58 or not seed:
        raise PreflightError(f"{config_path} is missing miner.ss58 / miner.seed")
    return ss58, seed


def _discover_profile_json(scripts_dir: str) -> str:
    """Run ``discover-profile.sh --json-only`` and return the profile JSON text.

    The script writes a JSON file and prints its path (last stdout line); we read it,
    then delete it — the profile is transient, only the POST needs it.
    """
    script = Path(scripts_dir) / "discover-profile.sh"
    if not script.exists():
        raise PreflightError(f"discover-profile.sh not found at {script}")
    result = proc.run(
        ["bash", str(script), "--json-only"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise PreflightError(
            f"discover-profile.sh failed: {result.stderr.strip() or 'no output'}"
        )
    lines = [ln for ln in result.stdout.splitlines() if ln.strip()]
    if not lines:
        raise PreflightError("discover-profile.sh produced no JSON file path")
    path = Path(lines[-1].strip())
    try:
        data = path.read_text()
    except OSError as exc:
        raise PreflightError(
            f"cannot read discover-profile output {path}: {exc}"
        ) from exc
    finally:
        try:
            path.unlink()
        except OSError:
            pass
    return data


def _apply_target_os(profile_json: str, target_os: str) -> str:
    """Return the profile rewritten as if this host were already on ``target_os``.

    Used by the pre-upgrade path (--target-os): the OS release is what picks the QEMU that
    generates the guest ACPI measured into RTMR0, so every OS-derived field has to move
    together. Rewriting the release while leaving the live host's QEMU behind would register
    the class against a (release, QEMU) pair that does not exist — e.g. a 25.10 host asking
    about 26.04 would submit "26.04 + QEMU 10.1.0", which 26.04 never ships.
    """
    qemu_version = SUPPORTED_QEMU_BY_OS.get(target_os)
    if qemu_version is None:
        raise PreflightError(
            f"target OS {target_os!r} is not supported {sorted(SUPPORTED_QEMU_BY_OS)}; "
            f"supported releases ship a QEMU whose RTMR0 is baselined."
        )
    try:
        doc = json.loads(profile_json)
    except json.JSONDecodeError as exc:
        raise PreflightError(
            f"discover-profile output is not valid JSON: {exc}"
        ) from exc
    ld = doc.get("launch_determinism")
    if not isinstance(ld, dict):
        raise PreflightError(
            "discover-profile output has no launch_determinism block to override"
        )
    ld["qemu_version"] = qemu_version
    # The distro build string of a QEMU we are not running is unknowable, so mark it as
    # projected rather than leaving the live host's (now contradictory) one in place.
    ld["qemu_version_full"] = (
        f"QEMU emulator version {qemu_version} (projected for target OS {target_os})"
    )
    ld["cpu_args"] = GUEST_CPU_ARGS
    host = doc.get("host")
    if isinstance(host, dict):
        host["os_version_id"] = target_os
    # Compact separators keep the signed body small; key order is irrelevant to the API.
    return json.dumps(doc, separators=(",", ":"))


def _sign(seed: str, body: bytes, nonce: str) -> "tuple[str, str]":
    """Sign ``{ss58}:{nonce}:{sha256(body)}`` with the miner hotkey (sr25519).

    Returns (ss58, signature_hex). The ss58 is derived from the seed (so it always matches
    the signature); the API verifies the signature against the hotkey header and confirms
    the hotkey is registered + un-blacklisted.
    """
    try:
        kp = Keypair.create_from_seed(seed, crypto_type=KeypairType.SR25519)
    except Exception as exc:
        raise PreflightError(f"invalid miner seed: {exc}") from exc
    body_hash = hashlib.sha256(body).hexdigest()
    signature = kp.sign(f"{kp.ss58_address}:{nonce}:{body_hash}")
    return kp.ss58_address, signature.hex()


def _post(
    path: str, api_base: str, hotkey: str, nonce: str, signature: str, body: bytes
) -> dict:
    """POST the signed profile body to ``path`` on the API; return the parsed JSON dict."""
    url = f"{api_base.rstrip('/')}{path}"
    req = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "User-Agent": "chutes-cvm-preflight/1.0",
            "X-Chutes-Hotkey": hotkey,
            "X-Chutes-Nonce": nonce,
            "X-Chutes-Signature": signature,
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:  # nosec B310
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        detail = f"HTTP {exc.code}"
        try:
            err = json.loads(exc.read().decode())
            detail = (
                err.get("detail") or err.get("message") or err.get("error") or detail
            )
        except Exception:  # nosec B110
            pass
        raise PreflightError(f"API rejected the request ({exc.code}): {detail}")
    except urllib.error.URLError as exc:
        raise PreflightError(f"API unreachable at {api_base}: {exc.reason}")
    except (ValueError, json.JSONDecodeError) as exc:
        raise PreflightError(f"API returned an unparseable response: {exc}")


def _signed_profile(
    config_path: str, scripts_dir: str, target_os: "str | None" = None
) -> "tuple[str, str, str, bytes]":
    """Discover this host's profile and sign it with the miner hotkey.

    Returns (hotkey, nonce, signature, body) for a POST. ``target_os`` rewrites the profile's
    OS-derived fields (release, QEMU, -cpu args) first, for the pre-upgrade check. Shared by the
    preflight check and the submit path.
    """
    ss58, seed = _load_miner_creds(config_path)
    profile_json = _discover_profile_json(scripts_dir)
    if target_os:
        profile_json = _apply_target_os(profile_json, target_os)
    body = profile_json.encode()
    nonce = str(int(time.time()))
    hotkey, signature = _sign(seed, body, nonce)
    if ss58 and hotkey != ss58:
        # Non-fatal: the seed is authoritative for the signature, but a mismatch means the
        # configured ss58 is wrong — surface it so the operator can fix the config.
        print(
            f"  warning: config miner.ss58 ({ss58}) does not match the seed's hotkey ({hotkey}); "
            "signing with the seed's hotkey."
        )
    return hotkey, nonce, signature, body


def run_preflight(
    config_path: str,
    scripts_dir: str,
    version: str,
    rc: bool,
    api_base: str = DEFAULT_API_BASE,
    target_os: "str | None" = None,
) -> dict:
    """Discover -> sign -> POST /servers/tdx/preflight -> verdict.

    Asks whether a published measurement for an image of ``(version, rc)`` covers this host class.
    Returns {fingerprint, launchable, detail}; raises PreflightError on any failure to reach a
    verdict (the caller fails closed)."""
    hotkey, nonce, signature, body = _signed_profile(
        config_path, scripts_dir, target_os
    )
    query = urlencode({"version": version, "rc": "true" if rc else "false"})
    return _post(
        f"/servers/tdx/preflight?{query}", api_base, hotkey, nonce, signature, body
    )


def run_host_class_status(
    config_path: str,
    scripts_dir: str,
    api_base: str = DEFAULT_API_BASE,
    target_os: "str | None" = None,
) -> dict:
    """Discover -> sign -> POST /servers/tdx/host_profiles/status -> host class verdict.

    The version-free question behind `host verify`: is this topology known, and which published
    images cover it? Deliberately takes no version — a host is verified before it has downloaded
    any image, so nothing here may depend on what is on disk.

    Returns {fingerprint, status, measurements: [{version, rc}, ...], detail}. An empty
    ``measurements`` means nothing can launch here yet; ``status`` (unknown/pending/accepted) says
    whether the miner must register the class or simply wait. Raises PreflightError on any failure
    to reach a verdict.
    """
    hotkey, nonce, signature, body = _signed_profile(
        config_path, scripts_dir, target_os
    )
    return _post(
        "/servers/tdx/host_profiles/status", api_base, hotkey, nonce, signature, body
    )


def submit_profile(
    config_path: str,
    scripts_dir: str,
    api_base: str = DEFAULT_API_BASE,
    target_os: "str | None" = None,
) -> dict:
    """Discover -> sign -> POST /servers/tdx/host_profiles -> register.

    Stores this host class so Chutes generates its measurements. Returns
    {fingerprint, status, stored, detail}; raises PreflightError on failure. Run when the preflight
    reports the class is not yet launchable. ``target_os`` registers the class the host will BE
    after an OS upgrade (target release + the QEMU it ships), not the one it is on now.
    """
    hotkey, nonce, signature, body = _signed_profile(
        config_path, scripts_dir, target_os
    )
    return _post("/servers/tdx/host_profiles", api_base, hotkey, nonce, signature, body)
