# sr25519

A minimal, Substrate-compatible sr25519 signer (`sr25519`) for the sek8s guest
initramfs.

**Status:** standalone and verified; not yet wired into the initramfs or the image build
pipeline. See [docs/specs/vm-boot-identity-tofu.md](../../docs/specs/vm-boot-identity-tofu.md)
for the design context.

## Why this exists

The guest initramfs needs to prove possession of the **miner hotkey** when it calls the
validator's boot-attestation endpoints. Without that proof, any miner running a genuine TDX
host on the published image can claim another miner's `(hotkey, vm_name)` pair, trigger a LUKS
passphrase rotation, and permanently brick the victim's VM.

The initramfs already stages `openssl` and uses it for RSA keygen, x509 self-signing, and
detached signatures (see `attest-common` and `rc-sign`). But sr25519 is Schnorr over
Ristretto25519, which openssl cannot do, and importing the bittensor/substrate Python stack
into a busybox initramfs is not viable.

This binary fills exactly that gap and nothing more: it is ~550 KB, statically linked, and
depends on three small crates.

## Usage

```
sr25519 address --seed-file <path>
sr25519 sign    --seed-file <path> (--message <str> | --message-file <path>)
sr25519 verify  --address <ss58> --signature <hex> (--message <str> | --message-file <path>)
```

The seed is read only from a file — never from argv — so it cannot leak through
`/proc/<pid>/cmdline`. `--message-file -` reads the message from stdin.

Signing the sek8s auth payload from the initramfs looks like:

```sh
HOTKEY=$(sr25519 address --seed-file /run/tdx-config/miner-seed)
SIG=$(sr25519 sign --seed-file /run/tdx-config/miner-seed \
        --message "${HOTKEY}:${NONCE}:${BODY_SHA256}")
```

Note that deriving `HOTKEY` from the seed rather than reading `/run/tdx-config/miner-ss58`
removes an existing failure mode: today nothing cross-checks the configured SS58 against the
seed, so a typo produces a VM that attests under the wrong hotkey while every `allow_miner`
route rejects its requests.

## Compatibility contract

Interoperability with `substrateinterface.Keypair` is load-bearing, and all three of these are
silent-failure traps if they drift:

1. **Seed expansion** is `MiniSecretKey::expand_to_keypair(ExpansionMode::Ed25519)` —
   Substrate's `sr25519_pair_from_seed`. `ExpansionMode::Uniform` yields a valid but *different*
   keypair, and therefore a different SS58 address.
2. **Signing context** is `signing_context(b"substrate")`. A different context produces
   signatures that never verify.
3. **SS58** uses network prefix 42 (generic Substrate/Bittensor — this is what makes addresses
   start with `5`) with a blake2b-512 checksum over `"SS58PRE" || payload`.

sr25519 signatures are **randomized**: the same message signs to different bytes every time and
all of them verify. Never assert on signature-byte equality.

## Build

```sh
make build-sr25519     # static musl binary, the one that ships
# or
cd src/sr25519 && cargo build --release --target x86_64-unknown-linux-musl
```

Static musl is deliberate: it has no glibc coupling to the guest base image, so the initramfs
hook can `copy_exec` a single file with no library closure to resolve.

The toolchain is pinned in `rust-toolchain.toml`. The binary is measured into the guest RTMR
once integrated, so a toolchain or dependency bump changes the measurement and must be a
deliberate, versioned change — bump `ansible/guest/VERSION` and republish measurements.

## Test

```sh
make test-sr25519
```

Two layers:

- `cargo test` — unit tests covering SS58 round-tripping, hex handling, the signing context,
  and a guard asserting that `ExpansionMode::Uniform` would *not* reproduce Alice's address.
- `tests/rust/test_sr25519.py` — cross-checks the built binary against the real
  `substrate-interface` library in both directions (Rust signs → Python verifies, Python signs
  → Rust verifies), across several seeds. These skip automatically when the binary has not been
  built.

## Not yet decided

- **Entropy in early initramfs.** Signing draws from `getrandom` for the witness. TDX guests
  have RDRAND/RDSEED, but this has not been measured at the point in boot where it would run,
  and `getrandom` can block on an uninitialized pool. If that turns out to be a problem,
  schnorrkel can be driven with a supplied (deterministic) RNG instead.
- **Distribution.** The intent is to commit a pre-built binary and copy it in at image build
  time — mirroring `tools/gpu-tools/` (recipe) → `chutes_cvm/scripts/gpu-tools/` (artifact) —
  rather than putting cargo in the image build. A network fetch from crates.io inside the
  measured build pipeline would be a source of measurement drift.
