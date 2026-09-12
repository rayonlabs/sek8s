"""Graceful VM shutdown via the guest system-manager API.

`chutes-cvm guest down` (without --force) asks the running guest to power itself off cleanly by POSTing a
signed request to the system-manager status API on the VM — the same endpoint the chutes-miner
control plane uses (``POST http://<vm_ip>:8080/status/system/shutdown``). This lets a miner shut a
VM down gracefully with only the miner hotkey in their config.yaml, no chutes-miner CLI required.
`--force` skips this and force-kills QEMU instead.

Auth matches sek8s_common.auth (server side): headers X-Chutes-Hotkey / X-Chutes-Nonce /
X-Chutes-Signature, with the signature over ``{ss58}:{nonce}:status`` (purpose-based, since the
POST has no body → the server uses ``purpose="status"``). The header/purpose strings are inlined
here so this standalone package keeps no dependency on the guest packages.
"""

from __future__ import annotations

import time
import urllib.error
import urllib.request

from chutes_cvm.guest.config import ConfigError, LaunchConfig
from substrateinterface import Keypair, KeypairType

# Contract mirrored from sek8s_common (constants + auth.authorize(purpose="status")) and the
# system-manager status API (mounted at /status, PORT=8080, REQUIRE_TLS=false → plain HTTP).
_HOTKEY_HEADER = "X-Chutes-Hotkey"
_NONCE_HEADER = "X-Chutes-Nonce"
_SIGNATURE_HEADER = "X-Chutes-Signature"
_STATUS_PORT = 8080
_SHUTDOWN_PATH = "/status/system/shutdown"
_PURPOSE = "status"


class ShutdownError(Exception):
    """The graceful shutdown could not be requested (message is user-facing)."""


def graceful_shutdown(config_path: "str | None", timeout: float = 10.0) -> str:
    """Ask the guest to power off cleanly via the system-manager API. Returns the VM IP on
    success; raises ShutdownError if the config/creds are missing or the API can't be reached.
    """
    try:
        cfg = LaunchConfig.from_file(config_path)
    except ConfigError as exc:
        raise ShutdownError(f"config: {exc}") from exc

    seed = cfg.miner.seed
    if not seed:
        raise ShutdownError(
            "config has no miner.seed — cannot sign the shutdown request (use --force to "
            "force-kill instead)."
        )
    vm_ip = cfg.network.vm_ip

    try:
        kp = Keypair.create_from_seed(seed, crypto_type=KeypairType.SR25519)
    except Exception as exc:
        raise ShutdownError(f"invalid miner seed: {exc}") from exc

    nonce = str(int(time.time()))
    signature = kp.sign(f"{kp.ss58_address}:{nonce}:{_PURPOSE}").hex()
    url = f"http://{vm_ip}:{_STATUS_PORT}{_SHUTDOWN_PATH}"
    req = urllib.request.Request(
        url,
        data=b"",
        method="POST",
        headers={
            _HOTKEY_HEADER: kp.ss58_address,
            _NONCE_HEADER: nonce,
            _SIGNATURE_HEADER: signature,
        },
    )
    print(f"Requesting graceful shutdown of the guest at {vm_ip} …")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # nosec B310
            resp.read()
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")[:200]
        raise ShutdownError(
            f"system-manager rejected the shutdown ({exc.code}): {detail}"
        ) from exc
    except urllib.error.URLError as exc:
        raise ShutdownError(
            f"could not reach the system-manager at {url}: {exc.reason} "
            "(is the VM running? use --force to force-kill)"
        ) from exc
    print("✓ Graceful shutdown requested — the guest will power off shortly.")
    return vm_ip
