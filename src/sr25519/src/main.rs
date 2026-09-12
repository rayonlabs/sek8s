//! `sr25519` — a minimal, Substrate-compatible sr25519 signer.
//!
//! Purpose: let the guest initramfs prove possession of the miner hotkey without
//! importing the bittensor/substrate Python stack. The busybox initramfs already
//! stages `openssl`, but sr25519 (Schnorr over Ristretto25519) is not something
//! openssl can do, so this binary fills exactly that gap and nothing more.
//!
//! Byte-for-byte compatibility with `substrateinterface.Keypair` is load-bearing
//! and rests on three conventions, each of which is a silent-failure trap if it
//! drifts. All three are pinned by the cross-check tests in
//! `tests/rust/test_sr25519.py`, which generate vectors with the real
//! Python library:
//!
//!   1. Seed expansion uses `MiniSecretKey::expand_to_keypair(ExpansionMode::Ed25519)`
//!      — Substrate's `sr25519_pair_from_seed`. `ExpansionMode::Uniform` also
//!      "works" but yields a different keypair, and therefore a different SS58.
//!   2. Signatures are made under `signing_context(b"substrate")`. A different
//!      context produces signatures that simply never verify.
//!   3. SS58 uses network prefix 42 (the generic Substrate/Bittensor prefix, which
//!      is what makes addresses start with '5') and a blake2b-512 checksum over
//!      `"SS58PRE" || payload`.
//!
//! Note that sr25519 signatures are randomized: signing the same message twice
//! yields different bytes, and both verify. Tests must assert on verification,
//! never on signature equality.
//!
//! The seed is only ever read from a file, never from argv, so it cannot leak via
//! `/proc/<pid>/cmdline` to anything else running in the initramfs.

use std::io::Read;
use std::process::ExitCode;

use blake2::{Blake2b512, Digest};
use schnorrkel::{signing_context, ExpansionMode, MiniSecretKey, PublicKey, Signature};

/// Substrate's signing context for sr25519. Must match `py-sr25519-bindings`.
const SIGNING_CONTEXT: &[u8] = b"substrate";
/// Domain separator prefixed to the payload before the SS58 checksum hash.
const SS58_PRE: &[u8] = b"SS58PRE";
/// Generic Substrate network prefix, as used by Bittensor. Yields '5'-prefixed addresses.
const SS58_NETWORK: u8 = 42;

const SEED_LEN: usize = 32;
const PUBKEY_LEN: usize = 32;
const SIGNATURE_LEN: usize = 64;
const SS58_CHECKSUM_LEN: usize = 2;

const USAGE: &str = "\
sr25519 — sign and verify Substrate/Bittensor account keys

Derives SS58 addresses, signs messages, and verifies signatures using sr25519
(Schnorr over Ristretto25519), the scheme Substrate and Bittensor use for account
keys. Output is byte-compatible with substrate-interface / py-sr25519-bindings.

Built for the sek8s guest initramfs, where the Python substrate stack cannot run.

USAGE:
    sr25519 address --seed-file <path>
    sr25519 sign    --seed-file <path> (--message <str> | --message-file <path>)
    sr25519 verify  --address <ss58> --signature <hex> (--message <str> | --message-file <path>)

VERBS:
    address   Derive and print the SS58 address (network 42) for a seed.
    sign      Print a hex-encoded sr25519 signature over the message.
    verify    Verify a hex signature against an SS58 address. Exit 0 = valid.

OPTIONS:
    --seed-file <path>     File holding the 32-byte seed as hex (64 chars, optional
                           0x prefix). Surrounding whitespace is trimmed. Required
                           for `address` and `sign`. Never passed on argv.
    --message <str>        Message to sign/verify, as a UTF-8 string.
    --message-file <path>  Message to sign/verify, as raw bytes. Use '-' for stdin.
                           Mutually exclusive with --message.
    --address <ss58>       SS58 address to verify against.
    --signature <hex>      Hex-encoded 64-byte signature to verify.
    -h, --help             Print this help.

Signatures are randomized: the same message signs to different bytes each time and
all of them verify. Compare by verifying, not by comparing signature bytes.
";

fn main() -> ExitCode {
    let args: Vec<String> = std::env::args().skip(1).collect();
    match run(&args) {
        Ok(output) => {
            if !output.is_empty() {
                println!("{output}");
            }
            ExitCode::SUCCESS
        }
        Err(err) => {
            eprintln!("sr25519: {err}");
            ExitCode::FAILURE
        }
    }
}

fn run(args: &[String]) -> Result<String, String> {
    if args.is_empty() || args[0] == "-h" || args[0] == "--help" {
        return Ok(USAGE.trim_end().to_string());
    }

    let verb = args[0].as_str();
    let opts = parse_options(&args[1..])?;

    match verb {
        "address" => {
            let keypair = keypair_from_seed_file(&require(&opts, "--seed-file")?)?;
            Ok(ss58_encode(&keypair.public.to_bytes()))
        }
        "sign" => {
            let keypair = keypair_from_seed_file(&require(&opts, "--seed-file")?)?;
            let message = read_message(&opts)?;
            let signature = keypair.sign(signing_context(SIGNING_CONTEXT).bytes(&message));
            Ok(hex_encode(&signature.to_bytes()))
        }
        "verify" => {
            let address = require(&opts, "--address")?;
            let signature_hex = require(&opts, "--signature")?;
            let message = read_message(&opts)?;

            let pubkey_bytes = ss58_decode(&address)?;
            let public = PublicKey::from_bytes(&pubkey_bytes)
                .map_err(|e| format!("invalid public key in address: {e}"))?;

            let signature_bytes = hex_decode(&signature_hex)?;
            if signature_bytes.len() != SIGNATURE_LEN {
                return Err(format!(
                    "signature must be {SIGNATURE_LEN} bytes ({} hex chars), got {}",
                    SIGNATURE_LEN * 2,
                    signature_bytes.len()
                ));
            }
            let signature = Signature::from_bytes(&signature_bytes)
                .map_err(|e| format!("malformed signature: {e}"))?;

            public
                .verify(signing_context(SIGNING_CONTEXT).bytes(&message), &signature)
                .map_err(|_| "signature verification failed".to_string())?;
            Ok("OK".to_string())
        }
        other => Err(format!(
            "unknown verb '{other}' (expected address, sign, or verify); try --help"
        )),
    }
}

// ── Argument handling ────────────────────────────────────────────────────────

/// Flat `--flag value` parsing. Deliberately not clap: this binary is copied into
/// a measured initramfs, so every dependency has to earn its place.
fn parse_options(args: &[String]) -> Result<Vec<(String, String)>, String> {
    let mut opts = Vec::new();
    let mut i = 0;
    while i < args.len() {
        let flag = &args[i];
        if !flag.starts_with("--") {
            return Err(format!("unexpected argument '{flag}'; try --help"));
        }
        let value = args
            .get(i + 1)
            .ok_or_else(|| format!("{flag} requires a value"))?;
        opts.push((flag.clone(), value.clone()));
        i += 2;
    }
    Ok(opts)
}

fn lookup(opts: &[(String, String)], flag: &str) -> Option<String> {
    opts.iter()
        .find(|(k, _)| k == flag)
        .map(|(_, v)| v.clone())
}

fn require(opts: &[(String, String)], flag: &str) -> Result<String, String> {
    lookup(opts, flag).ok_or_else(|| format!("{flag} is required"))
}

/// Message bytes from `--message` (UTF-8) or `--message-file` (raw, '-' = stdin).
/// Exactly one must be given, so an empty message is always explicit rather than
/// the silent result of a forgotten flag.
fn read_message(opts: &[(String, String)]) -> Result<Vec<u8>, String> {
    let inline = lookup(opts, "--message");
    let from_file = lookup(opts, "--message-file");

    match (inline, from_file) {
        (Some(_), Some(_)) => Err("--message and --message-file are mutually exclusive".to_string()),
        (Some(text), None) => Ok(text.into_bytes()),
        (None, Some(path)) => {
            if path == "-" {
                let mut buf = Vec::new();
                std::io::stdin()
                    .read_to_end(&mut buf)
                    .map_err(|e| format!("failed to read message from stdin: {e}"))?;
                Ok(buf)
            } else {
                std::fs::read(&path).map_err(|e| format!("failed to read {path}: {e}"))
            }
        }
        (None, None) => Err("one of --message or --message-file is required".to_string()),
    }
}

// ── Key handling ─────────────────────────────────────────────────────────────

/// Read a hex seed from `path` and expand it the way Substrate does.
///
/// `ExpansionMode::Ed25519` is not a stylistic choice: it is what
/// `sr25519_pair_from_seed` uses, and picking `Uniform` instead silently yields a
/// valid-but-different keypair whose SS58 will not match the miner's hotkey.
fn keypair_from_seed_file(path: &str) -> Result<schnorrkel::Keypair, String> {
    let raw = std::fs::read_to_string(path).map_err(|e| format!("failed to read {path}: {e}"))?;
    let seed = hex_decode(raw.trim())?;
    if seed.len() != SEED_LEN {
        return Err(format!(
            "seed must be {SEED_LEN} bytes ({} hex chars), got {} bytes",
            SEED_LEN * 2,
            seed.len()
        ));
    }
    let mini =
        MiniSecretKey::from_bytes(&seed).map_err(|e| format!("invalid sr25519 seed: {e}"))?;
    Ok(mini.expand_to_keypair(ExpansionMode::Ed25519))
}

// ── SS58 ─────────────────────────────────────────────────────────────────────

/// `base58( prefix || pubkey || blake2b_512("SS58PRE" || prefix || pubkey)[..2] )`
fn ss58_encode(pubkey: &[u8; PUBKEY_LEN]) -> String {
    let mut payload = Vec::with_capacity(1 + PUBKEY_LEN + SS58_CHECKSUM_LEN);
    payload.push(SS58_NETWORK);
    payload.extend_from_slice(pubkey);

    let checksum = ss58_checksum(&payload);
    payload.extend_from_slice(&checksum);

    bs58::encode(payload).into_string()
}

fn ss58_decode(address: &str) -> Result<[u8; PUBKEY_LEN], String> {
    let decoded = bs58::decode(address.trim())
        .into_vec()
        .map_err(|e| format!("address is not valid base58: {e}"))?;

    let expected_len = 1 + PUBKEY_LEN + SS58_CHECKSUM_LEN;
    if decoded.len() != expected_len {
        return Err(format!(
            "address decodes to {} bytes, expected {expected_len}",
            decoded.len()
        ));
    }
    if decoded[0] != SS58_NETWORK {
        return Err(format!(
            "address has network prefix {}, expected {SS58_NETWORK}",
            decoded[0]
        ));
    }

    let (payload, checksum) = decoded.split_at(1 + PUBKEY_LEN);
    if ss58_checksum(payload) != checksum {
        return Err("address checksum mismatch (typo in the SS58?)".to_string());
    }

    let mut pubkey = [0u8; PUBKEY_LEN];
    pubkey.copy_from_slice(&payload[1..]);
    Ok(pubkey)
}

fn ss58_checksum(payload: &[u8]) -> [u8; SS58_CHECKSUM_LEN] {
    let mut hasher = Blake2b512::new();
    hasher.update(SS58_PRE);
    hasher.update(payload);
    let digest = hasher.finalize();

    let mut checksum = [0u8; SS58_CHECKSUM_LEN];
    checksum.copy_from_slice(&digest[..SS58_CHECKSUM_LEN]);
    checksum
}

// ── Hex ──────────────────────────────────────────────────────────────────────

fn hex_encode(bytes: &[u8]) -> String {
    let mut out = String::with_capacity(bytes.len() * 2);
    for byte in bytes {
        out.push(nibble_to_char(byte >> 4));
        out.push(nibble_to_char(byte & 0x0f));
    }
    out
}

fn nibble_to_char(nibble: u8) -> char {
    match nibble {
        0..=9 => (b'0' + nibble) as char,
        _ => (b'a' + nibble - 10) as char,
    }
}

/// Decode hex, tolerating an optional `0x` prefix. The Ansible host role already
/// strips `0x` from `secretSeed`, but a hand-written config.yaml may not.
fn hex_decode(input: &str) -> Result<Vec<u8>, String> {
    let trimmed = input.trim();
    let body = trimmed
        .strip_prefix("0x")
        .or_else(|| trimmed.strip_prefix("0X"))
        .unwrap_or(trimmed);

    if body.len() % 2 != 0 {
        return Err(format!(
            "hex input has odd length {} (expected an even number of chars)",
            body.len()
        ));
    }

    let chars: Vec<char> = body.chars().collect();
    let mut bytes = Vec::with_capacity(chars.len() / 2);
    for pair in chars.chunks(2) {
        let hi = char_to_nibble(pair[0])?;
        let lo = char_to_nibble(pair[1])?;
        bytes.push((hi << 4) | lo);
    }
    Ok(bytes)
}

fn char_to_nibble(c: char) -> Result<u8, String> {
    match c {
        '0'..='9' => Ok(c as u8 - b'0'),
        'a'..='f' => Ok(c as u8 - b'a' + 10),
        'A'..='F' => Ok(c as u8 - b'A' + 10),
        _ => Err(format!("invalid hex character '{c}'")),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    // Alice's well-known development seed. Her SS58 under network 42 is a fixed,
    // widely published value, which makes it a usable hardcoded vector for the
    // seed-expansion + SS58 path without needing the Python library.
    const ALICE_SEED: &str = "e5be9a5092b81bca64be81d212e7f2f9eba183bb7a90954f7b76361f6edb5c0a";
    const ALICE_SS58: &str = "5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY";

    fn alice_keypair() -> schnorrkel::Keypair {
        let seed = hex_decode(ALICE_SEED).unwrap();
        MiniSecretKey::from_bytes(&seed)
            .unwrap()
            .expand_to_keypair(ExpansionMode::Ed25519)
    }

    #[test]
    fn ed25519_expansion_reproduces_alices_address() {
        let keypair = alice_keypair();
        assert_eq!(ss58_encode(&keypair.public.to_bytes()), ALICE_SS58);
    }

    #[test]
    fn uniform_expansion_would_be_wrong() {
        // Guards against someone "simplifying" the expansion mode later.
        let seed = hex_decode(ALICE_SEED).unwrap();
        let uniform = MiniSecretKey::from_bytes(&seed)
            .unwrap()
            .expand_to_keypair(ExpansionMode::Uniform);
        assert_ne!(ss58_encode(&uniform.public.to_bytes()), ALICE_SS58);
    }

    #[test]
    fn ss58_round_trips() {
        let keypair = alice_keypair();
        let pubkey = keypair.public.to_bytes();
        assert_eq!(ss58_decode(&ss58_encode(&pubkey)).unwrap(), pubkey);
    }

    #[test]
    fn ss58_rejects_a_corrupted_address() {
        let mut corrupted: Vec<char> = ALICE_SS58.chars().collect();
        corrupted[10] = if corrupted[10] == 'A' { 'B' } else { 'A' };
        let corrupted: String = corrupted.into_iter().collect();
        assert!(ss58_decode(&corrupted).is_err());
    }

    #[test]
    fn signature_verifies_under_the_substrate_context() {
        let keypair = alice_keypair();
        let message = b"5Grwva:1756600000:status";
        let signature = keypair.sign(signing_context(SIGNING_CONTEXT).bytes(message));
        assert!(keypair
            .public
            .verify(signing_context(SIGNING_CONTEXT).bytes(message), &signature)
            .is_ok());
    }

    #[test]
    fn signature_does_not_verify_under_a_different_context() {
        let keypair = alice_keypair();
        let message = b"payload";
        let signature = keypair.sign(signing_context(SIGNING_CONTEXT).bytes(message));
        assert!(keypair
            .public
            .verify(signing_context(b"not-substrate").bytes(message), &signature)
            .is_err());
    }

    #[test]
    fn signatures_are_randomized_but_both_verify() {
        let keypair = alice_keypair();
        let message = b"same message";
        let first = keypair.sign(signing_context(SIGNING_CONTEXT).bytes(message));
        let second = keypair.sign(signing_context(SIGNING_CONTEXT).bytes(message));
        assert_ne!(first.to_bytes(), second.to_bytes());
        for signature in [first, second] {
            assert!(keypair
                .public
                .verify(signing_context(SIGNING_CONTEXT).bytes(message), &signature)
                .is_ok());
        }
    }

    #[test]
    fn hex_round_trips_and_tolerates_0x() {
        let bytes = vec![0x00, 0x0f, 0xa5, 0xff];
        let encoded = hex_encode(&bytes);
        assert_eq!(encoded, "000fa5ff");
        assert_eq!(hex_decode(&encoded).unwrap(), bytes);
        assert_eq!(hex_decode("0x000fa5ff").unwrap(), bytes);
        assert_eq!(hex_decode("  000FA5FF\n").unwrap(), bytes);
    }

    #[test]
    fn hex_rejects_malformed_input() {
        assert!(hex_decode("abc").is_err());
        assert!(hex_decode("zz").is_err());
    }
}
