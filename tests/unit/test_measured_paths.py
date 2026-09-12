"""Guard: security-critical privileged boot scripts and policy files must stay in
the RTMR3 measured-path list, so they cannot be swapped/added on the root image
without changing the measurement. Catches accidental removal of a measured path.
"""

from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
MINER_CONF = REPO / "ansible/guest/roles/rtmr3-measure/files/tdx-measure-miner.conf"


def _measured_paths(conf: Path):
    return {
        ln.strip()
        for ln in conf.read_text().splitlines()
        if ln.strip() and not ln.strip().startswith("#")
    }


def test_privileged_boot_paths_are_measured():
    measured = _measured_paths(MINER_CONF)
    required = {
        # Mints the per-VM admission CA each boot and injects its caBundle into the
        # webhook manifests — the admission webhook TLS trust anchor.
        "/usr/local/bin/generate-admission-cert",
        # Debug flag that flips prod fail-poweroff to warn-only; absent on prod, so
        # measuring it makes injecting it on a prod image RTMR3-detectable.
        "/etc/default/generate-admission-cert",
        # Path-restricted root `rm` wrapper the system-manager sudoers grant targets.
        "/usr/local/bin/cache-rm",
        # Runtime integrity verifier and the storage sync that feeds it.
        "/usr/local/bin/rtmr3-verify",
        "/usr/local/bin/setup-storage-bind-mounts.sh",
        # Measured helm chart specs (versions/values/flags) and OPA policies.
        "/etc/chutes/charts",
        "/etc/opa/policies",
        # Log shipper config: the validator URL logs egress to, the CRI endpoint pod
        # metadata comes from, and the names deciding which pods are captured at all.
        "/etc/chute-log-shipper",
        # The shared libraries every confined service mmap-execs, plus the kernel
        # modules on disk. Measuring /usr/bin without this covers execve targets
        # only, which is half the executable surface.
        "/usr/lib",
    }
    missing = required - measured
    assert (
        not missing
    ), f"security-critical paths missing from RTMR3 measured list: {sorted(missing)}"
