VALIDATOR_HEADER = "X-Chutes-Validator"
HOTKEY_HEADER = "X-Chutes-Hotkey"
MINER_HEADER = "X-Chutes-Miner"
NONCE_HEADER = "X-Chutes-Nonce"
SIGNATURE_HEADER = "X-Chutes-Signature"
NONCE_MAX_AGE_SECONDS = 30

# The VM's single mTLS client identity, minted at boot by the vm-tls role. It is presented for
# ALL mTLS between the VM and Chutes (registry.chutes.ai pulls, cvm.chutes.ai log/API egress, every
# CVM->API call); the API validates it against the per-VM CA registered at /provision. One source of
# truth for the path so a role change touches one place, not each consumer. (The on-disk dir is named
# registry-tls/ for legacy reasons — the identity is not registry-specific.)
MTLS_CLIENT_CERT = "/run/chutes/registry-tls/client.crt"
MTLS_CLIENT_KEY = "/run/chutes/registry-tls/client.key"
