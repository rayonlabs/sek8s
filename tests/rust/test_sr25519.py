"""Cross-check the sr25519 binary against substrate-interface.

The Rust binary exists so the guest initramfs can prove possession of the miner
hotkey without the Python substrate stack. That is only useful if it produces
*exactly* what `substrateinterface.Keypair` produces, so these tests drive the
real library as the oracle rather than asserting against hardcoded vectors.

Skipped when the binary has not been built; build it with:

    cd src/sr25519 && cargo build --release
"""

import hashlib
import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest
from substrateinterface import Keypair, KeypairType

REPO_ROOT = Path(__file__).resolve().parents[2]
CRATE_DIR = REPO_ROOT / "src" / "sr25519"

# Well-known Substrate development seeds, plus one arbitrary seed, so a bug that
# only shows up for particular scalar values has a chance to surface.
SEEDS = [
    "e5be9a5092b81bca64be81d212e7f2f9eba183bb7a90954f7b76361f6edb5c0a",  # //Alice
    "398f0c28f98885e046333d4a41c19cee4c37368a9832c6502f6cfd182e2aef89",  # //Bob
    "0000000000000000000000000000000000000000000000000000000000000001",
    "7f7f7f7f7f7f7f7f7f7f7f7f7f7f7f7f7f7f7f7f7f7f7f7f7f7f7f7f7f7f7f7f",
]


def _find_binary() -> str | None:
    """Locate sr25519: explicit override, cargo output dir, then PATH."""
    override = os.environ.get("SR25519_BIN")
    if override:
        return override if Path(override).is_file() else None
    # The static musl build is the artifact that actually ships into the initramfs,
    # so prefer it: a passing run against target/release proves less than one against
    # the binary that gets measured.
    candidates = [
        CRATE_DIR / "target" / "x86_64-unknown-linux-musl" / "release" / "sr25519",
        CRATE_DIR / "target" / "release" / "sr25519",
        CRATE_DIR / "target" / "debug" / "sr25519",
    ]
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)
    return shutil.which("sr25519")


BINARY = _find_binary()

pytestmark = pytest.mark.skipif(
    BINARY is None,
    reason="sr25519 not built (cd src/sr25519 && cargo build --release)",
)


def run(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    result = subprocess.run([BINARY, *args], capture_output=True, text=True, timeout=30)
    if check and result.returncode != 0:
        raise AssertionError(f"sr25519 {args} failed: {result.stderr.strip()}")
    return result


@pytest.fixture
def seed_file(tmp_path):
    """Write a seed to a 0600 file and return its path, as the initramfs will."""

    def _write(seed_hex: str) -> str:
        path = tmp_path / "miner-seed"
        path.write_text(seed_hex)
        path.chmod(0o600)
        return str(path)

    return _write


@pytest.mark.parametrize("seed", SEEDS)
def test_address_matches_substrate_interface(seed, seed_file):
    """Seed expansion + SS58 encoding agree with the Python library.

    This is the check that catches ExpansionMode::Uniform being used by mistake:
    it produces a valid address that is simply not the miner's hotkey.
    """
    expected = Keypair.create_from_seed(
        f"0x{seed}", crypto_type=KeypairType.SR25519
    ).ss58_address
    assert run("address", "--seed-file", seed_file(seed)).stdout.strip() == expected


@pytest.mark.parametrize("seed", SEEDS)
def test_rust_signature_verifies_in_python(seed, seed_file):
    """A signature the initramfs makes must verify server-side (Python)."""
    keypair = Keypair.create_from_seed(f"0x{seed}", crypto_type=KeypairType.SR25519)
    message = f"{keypair.ss58_address}:{int(time.time())}:attest"

    signature = run(
        "sign", "--seed-file", seed_file(seed), "--message", message
    ).stdout.strip()

    assert len(signature) == 128
    assert keypair.verify(message, bytes.fromhex(signature))


@pytest.mark.parametrize("seed", SEEDS)
def test_python_signature_verifies_in_rust(seed, seed_file):
    """The inverse direction, so `verify` is usable as a self-test on the host."""
    keypair = Keypair.create_from_seed(f"0x{seed}", crypto_type=KeypairType.SR25519)
    message = "provision:vm-01"
    signature = keypair.sign(message).hex()

    result = run(
        "verify",
        "--address",
        keypair.ss58_address,
        "--signature",
        signature,
        "--message",
        message,
    )
    assert result.stdout.strip() == "OK"


def test_signs_the_sek8s_auth_message_shape(seed_file):
    """Exercise the real `{ss58}:{nonce}:{sha256(body)}` payload from services/util.py."""
    seed = SEEDS[0]
    keypair = Keypair.create_from_seed(f"0x{seed}", crypto_type=KeypairType.SR25519)
    body = b'{"quote":"...","vm_name":"chutes-01","first_boot":true}'
    message = f"{keypair.ss58_address}:1756600000:{hashlib.sha256(body).hexdigest()}"

    signature = run(
        "sign", "--seed-file", seed_file(seed), "--message", message
    ).stdout.strip()
    assert keypair.verify(message, bytes.fromhex(signature))


def test_tampered_message_fails_verification(seed_file):
    seed = SEEDS[0]
    keypair = Keypair.create_from_seed(f"0x{seed}", crypto_type=KeypairType.SR25519)
    signature = run(
        "sign", "--seed-file", seed_file(seed), "--message", "original"
    ).stdout.strip()

    result = run(
        "verify",
        "--address",
        keypair.ss58_address,
        "--signature",
        signature,
        "--message",
        "tampered",
        check=False,
    )
    assert result.returncode != 0
    assert "verification failed" in result.stderr


def test_signature_from_a_different_seed_is_rejected(seed_file):
    """The whole point: a VM without the seed cannot sign for that hotkey."""
    victim = Keypair.create_from_seed(f"0x{SEEDS[0]}", crypto_type=KeypairType.SR25519)
    message = f"{victim.ss58_address}:1756600000:attest"

    # Attacker claims the victim's address but signs with their own seed.
    attacker_signature = run(
        "sign", "--seed-file", seed_file(SEEDS[1]), "--message", message
    ).stdout.strip()

    result = run(
        "verify",
        "--address",
        victim.ss58_address,
        "--signature",
        attacker_signature,
        "--message",
        message,
        check=False,
    )
    assert result.returncode != 0


def test_message_can_be_read_from_stdin(seed_file):
    """--message-file - keeps large or binary payloads off argv."""
    seed = SEEDS[0]
    keypair = Keypair.create_from_seed(f"0x{seed}", crypto_type=KeypairType.SR25519)
    message = b"bytes-from-stdin"

    result = subprocess.run(
        [BINARY, "sign", "--seed-file", seed_file(seed), "--message-file", "-"],
        input=message,
        capture_output=True,
        timeout=30,
    )
    assert result.returncode == 0
    assert keypair.verify(message, bytes.fromhex(result.stdout.decode().strip()))


def test_signatures_are_randomized_and_all_verify(seed_file):
    """sr25519 witnesses are random; never assert on signature-byte equality."""
    seed = SEEDS[0]
    keypair = Keypair.create_from_seed(f"0x{seed}", crypto_type=KeypairType.SR25519)
    path = seed_file(seed)

    signatures = {
        run("sign", "--seed-file", path, "--message", "repeat").stdout.strip()
        for _ in range(3)
    }
    assert len(signatures) == 3
    for signature in signatures:
        assert keypair.verify("repeat", bytes.fromhex(signature))


@pytest.mark.parametrize(
    "seed_content,expected_error",
    [
        ("deadbeef", "seed must be 32 bytes"),
        ("zz" * 32, "invalid hex character"),
        ("ab" * 33, "seed must be 32 bytes"),
    ],
)
def test_malformed_seed_is_rejected(seed_content, expected_error, seed_file):
    result = run("address", "--seed-file", seed_file(seed_content), check=False)
    assert result.returncode != 0
    assert expected_error in result.stderr


def test_seed_is_accepted_with_0x_prefix(seed_file):
    """The Ansible role strips 0x, but a hand-written config.yaml may not."""
    expected = Keypair.create_from_seed(
        f"0x{SEEDS[0]}", crypto_type=KeypairType.SR25519
    ).ss58_address
    assert (
        run("address", "--seed-file", seed_file(f"0x{SEEDS[0]}")).stdout.strip()
        == expected
    )


def test_missing_required_flag_errors_clearly():
    result = run("sign", "--message", "x", check=False)
    assert result.returncode != 0
    assert "--seed-file is required" in result.stderr


def test_help_lists_the_verbs():
    output = run("--help").stdout
    for verb in ("address", "sign", "verify"):
        assert verb in output
