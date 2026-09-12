"""Static checks on the guest AppArmor profiles.

These run the real `apparmor_parser` over each profile (skipped where the binary
is absent) and pin the one rule whose absence broke boot: `sek8s.deny-sensitive-default`
auto-attaches to /usr/bin/bash, so every k3s cluster-init step — which the post-start
wrapper invokes as `bash <script>` — runs confined by it. Under `abi <abi/4.0>` D-Bus
method calls are mediated separately from the socket, so without explicit dbus rules
`systemctl is-active k3s` inside a step failed with "Failed to connect to bus:
Permission denied" while the same call in the unconfined wrapper succeeded.
"""

import shutil
import subprocess
from pathlib import Path

import pytest
from jinja2 import Template

REPO = Path(__file__).resolve().parents[2]
ROLE = REPO / "ansible/guest/roles/apparmor-hardening"
PROFILE_DIR = ROLE / "files/profiles"
ABSTRACTION_DIR = ROLE / "templates/abstractions"
CLEANUP = REPO / "ansible/guest/roles/cleanup-orchestration"

PROFILES = [
    "sek8s.system-manager",
    "sek8s.setup-cache",
    "sek8s.deny-sensitive-default",
    "sek8s.attestation-proxy",
    "sek8s.chute-log-shipper",
]


@pytest.fixture(scope="module")
def include_dir(tmp_path_factory):
    """Render the Jinja abstractions so the parser can resolve the includes."""
    root = tmp_path_factory.mktemp("apparmor")
    out = root / "abstractions"
    out.mkdir()
    for name in ("sek8s-cache-deny", "sek8s-secrets-deny", "sek8s-shell-base"):
        template = Template((ABSTRACTION_DIR / f"{name}.j2").read_text())
        (out / name).write_text(template.render(debug_build=False))
    return root


@pytest.mark.parametrize("profile", PROFILES)
def test_profile_parses(profile, include_dir):
    parser = shutil.which("apparmor_parser")
    if parser is None:
        pytest.skip("apparmor_parser not available")

    result = subprocess.run(
        [parser, "-Q", "-K", "-I", str(include_dir), str(PROFILE_DIR / profile)],
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr


def test_verifier_covers_every_installed_profile():
    """The boot verifier powers off on a missing profile — keep its list in sync."""
    verifier = (ROLE / "files/verify-apparmor-profiles.sh").read_text()
    for profile in PROFILES:
        assert profile in verifier


def effective_policy(profile_name, include_dir):
    """Profile text plus its rendered sek8s-* includes.

    Rules live in abstractions as often as in the profile — sek8s-shell-base carries the D-Bus
    grants and the capability denies — so asserting against the profile file alone tests the wrong
    thing. What is enforced is the composition.
    """
    text = (PROFILE_DIR / profile_name).read_text()
    for inc in (include_dir / "abstractions").iterdir():
        if f"include <abstractions/{inc.name}>" in text:
            text += "\n" + inc.read_text()
    return text


def test_default_profile_allows_systemd_dbus(include_dir):
    """Confined shells must be able to run `systemctl is-active`.

    The k3s cluster-init steps depend on it; denying it restart-looped the VM.
    """
    policy = effective_policy("sek8s.deny-sensitive-default", include_dir)
    assert "peer=(name=org.freedesktop.systemd1)" in policy
    assert "peer=(name=org.freedesktop.DBus)" in policy
    # The AF_UNIX rule is required alongside the dbus rules under abi <abi/4.0>; without it the
    # connect() fails before any method call is mediated.
    assert "unix (connect, send, receive) type=stream" in policy


def test_default_profile_still_denies_the_sensitive_paths(include_dir):
    """The D-Bus grant must not have widened what this profile exists to block."""
    policy = effective_policy("sek8s.deny-sensitive-default", include_dir)
    assert "include <abstractions/sek8s-cache-deny>" in policy
    assert "include <abstractions/sek8s-secrets-deny>" in policy
    for capability in ("sys_module", "mac_admin", "mac_override", "sys_rawio"):
        assert f"deny capability {capability}," in policy
    # sys_boot is denied in the profile itself, not the shared base: measured init scripts need it
    # for their fail-closed poweroff, a stray shell must not have it.
    assert (
        "deny capability sys_boot,"
        in (PROFILE_DIR / "sek8s.deny-sensitive-default").read_text()
    )


def test_setup_cache_grants_no_dac_bypass():
    """setup-cache must not need a DAC bypass to walk the cache.

    The walk runs as uid 0 over a tree owned by uid 1000, so root sits in the "other" class and any
    directory without o+rx stops it. The fix is ordering (see below), not a capability: chmod -R
    repairs each directory before the walk descends. Granting dac_override or dac_read_search would
    paper over that and widen the profile for nothing.
    """
    profile = (PROFILE_DIR / "sek8s.setup-cache").read_text()
    assert "capability dac_override," not in profile
    assert "capability dac_read_search," not in profile


def test_setup_cache_repairs_modes_before_changing_ownership():
    """chmod -R must precede chown -R on the cache tree.

    chmod repairs each directory's mode on the pre-order visit so the walk can descend; chown
    repairs nothing, so running it first aborts on any directory the unit cannot read. The
    capability grant above also covers this, but the ordering must not silently regress.
    """
    script = (
        REPO / "ansible/guest/roles/cache-volume/files/setup-cache.sh"
    ).read_text()
    chmod_at = script.index('chmod -R 2775 "$SNAP_CACHE"')
    chown_at = script.index('chown -R "$SNAP_OWNER" "$SNAP_CACHE"')
    assert (
        chmod_at < chown_at
    ), "chown -R runs before chmod -R; an unreadable dir will brick boot"


def test_verifier_is_anchored_by_a_measured_drop_in():
    """The enforce check must be reachable without its multi-user.target.wants symlink.

    RTMR3 measures regular files only (`find -type f` in the initramfs measurer,
    `is_file() and not is_symlink()` in rtmr3-verify), so systemd enablement
    symlinks are unmeasured: deleting one silently disables a measured unit. The
    verifier therefore needs a hard Requires= from a unit whose drop-in lives
    under /etc/systemd/system, which is measured — the same anchoring
    rtmr3-verify.service already has.
    """
    unit = "verify-apparmor-profiles.service"
    dropins = [
        CLEANUP / "files/k3s.service.d/apparmor-verify.conf",
        CLEANUP / "files/system-manager.service.d/runtime-dependencies.conf",
    ]

    for dropin in dropins:
        text = dropin.read_text()
        assert f"Requires={unit}" in text, f"{dropin.name} lost the hard dependency"
        assert f"After={unit}" in text, f"{dropin.name} lost the ordering"

    # Each drop-in is worthless unless the cleanup role actually installs it.
    installed = (CLEANUP / "tasks/k3s-drop-ins.yml").read_text()
    assert "files/k3s.service.d/apparmor-verify.conf" in installed


def test_measured_paths_cover_the_drop_in_directories():
    """/etc/systemd/system must stay measured, or the anchoring above is inert."""
    conf = (
        REPO / "ansible/guest/roles/rtmr3-measure/files/tdx-measure-miner.conf"
    ).read_text()
    entries = {
        line.split("#", 1)[0].strip()
        for line in conf.splitlines()
        if line.split("#", 1)[0].strip()
    }
    assert "/etc/systemd/system" in entries


def test_run_chutes_perms_units_do_not_run_through_a_confined_bin():
    """A unit fixing up permissions under /run/chutes must exec its tools directly.

    sek8s.deny-sensitive-default auto-attaches to @{confined_bins} and denies
    `/run/chutes/ r`, so wrapping in `/bin/sh -c` pulls the unit into a profile written
    for a stray or escaped shell -- not for a measured oneshot with a fixed ExecStart.
    Under it any recursive walk fails, because enumerating a directory needs a read,
    and whether that bites depends on whether the unit beat apparmor.service to it.
    Exec'ing chgrp/chmod directly sidesteps the question: they are not in the
    attachment list, so the unit behaves the same either way.

    This fails if a shell wrapper appears OR if one of these tools is ever added to
    @{confined_bins}, which is the drift that would silently reintroduce the race.
    """
    profile = (PROFILE_DIR / "sek8s.deny-sensitive-default").read_text()
    declaration = next(
        line for line in profile.splitlines() if line.startswith("@{confined_bins} =")
    )
    confined = set()
    for token in declaration.split("=", 1)[1].split():
        if "{" in token:
            stem, names = token.split("{", 1)
            confined.update(stem + name for name in names.rstrip("}").split(","))
        else:
            confined.add(token)
    assert "/bin/sh" in confined, "confined_bins parse failed"

    units = [
        p
        for p in (REPO / "ansible/guest/roles").rglob("*.service")
        if any(
            line.startswith("ExecStart") and "/run/chutes" in line
            for line in p.read_text().splitlines()
        )
    ]
    assert {p.name for p in units} == {
        "registry-tls-config.service"
    }, "a new /run/chutes perms unit appeared; confirm it execs its tools directly"

    for unit in units:
        for line in unit.read_text().splitlines():
            if not line.startswith("ExecStart"):
                continue
            binary = line.split("=", 1)[1].split()[0].lstrip("-@:+!")
            assert (
                binary not in confined
            ), f"{unit.name}: {binary} is in @{{confined_bins}}; reads under /run/chutes are denied"


def test_signing_keys_need_no_group_or_perms_unit():
    """The fetched signing keys are public, so nothing re-permissions them after boot.

    They are cosign/GPG *public* keys, RSA-verified against the measured root key in
    the initramfs and written 0644 under a 0755 directory there. A `chutes-keys` group
    and a boot-time chgrp unit once existed for them; both gated access to data that
    was already world-readable, so their only effect was an occasionally-failing unit.
    Reintroducing either means reintroducing that -- and a supplementary GID would not
    map into admission-controller's PrivateUsers= namespace anyway.
    """
    role = REPO / "ansible/guest/roles/signing-keys"
    assert not (
        role / "files/signing-keys-config.service"
    ).exists(), "the perms unit is back; the keys are public and need no chgrp"

    for path in role.rglob("*"):
        if path.is_file():
            assert (
                "chutes-keys" not in path.read_text()
            ), f"{path.name} references the removed chutes-keys group"

    drop_in = (role / "files/admission-controller-signing-keys.conf").read_text()
    assert "SupplementaryGroups" not in drop_in
    assert "BindReadOnlyPaths=-/run/chutes/signing-keys" in drop_in


def test_run_chutes_is_created_unreachable_at_every_site():
    """/run/chutes must be created 0700 by every initramfs script that creates it.

    Nothing reaches it by ambient traversal. The three non-root services that need
    something from it -- admission-controller, chute-log-shipper, attestation-service --
    each mount a private tmpfs over /run/chutes in their own namespace and bind in only
    their subtree, so every reach is declared in a unit file rather than granted by the
    directory mode. system-manager needs nothing: its EnvironmentFile is read by PID 1
    as root before the drop to User=.

    The mode used to be decided by whichever script created the directory first, and the
    winner set it only by accident: `mkdir -m 700 -p /run/chutes/vm-root-ca` applies -m to
    the FINAL component only, so the intermediate /run/chutes landed at the umask default
    of 0755 and every later `mkdir -m 700 -p /run/chutes` was a no-op.
    """
    creators = {}
    for script in sorted((REPO / "ansible/guest/roles").glob("*/files/initramfs/*")):
        if not script.is_file():
            continue
        for number, line in enumerate(script.read_text().splitlines(), 1):
            stripped = line.strip()
            if stripped.startswith("mkdir") and stripped.endswith("/run/chutes"):
                creators[f"{script.name}:{number}"] = stripped

    assert creators, "no /run/chutes creation sites found; did the scripts move?"
    for where, command in creators.items():
        assert command == "mkdir -m 0700 -p /run/chutes", (
            f"{where}: {command!r} -- every site must set 0700 explicitly so no boot "
            f"ordering can decide the mode"
        )


def _split_shipper_rules():
    """The shipper profile's rule lines, split into (parent-only, child-only).

    Rules only -- comments are dropped, because the parent's comments legitimately name
    the paths the child owns when explaining why they are not granted here.
    """
    text = (PROFILE_DIR / "sek8s.chute-log-shipper").read_text()
    outer, marker, child = text.partition("profile crictl")
    assert marker, "the crictl child profile is gone"

    def rules(block):
        return [
            line.strip()
            for line in block.splitlines()
            if line.strip() and not line.strip().startswith("#")
        ]

    return rules(outer), rules(child)


def test_cri_socket_is_reachable_only_from_the_crictl_child():
    """The containerd socket must live in the child profile, not the shipper's own.

    The CRI socket is full control of containerd -- pull, run and exec any image, with no
    admission or cosign in the path. crictl-pods-helper allowlists two read-only verbs, but
    an `rix` grant makes that decorative: rix inherits the parent profile, so a process that
    can exec /usr/local/bin/k3s directly never passes through the wrapper at all. The
    boundary only exists if the transition is `cx ->` and the socket plus the k3s binary are
    granted solely inside the child.

    That matters here because the shipper's input is attacker-controlled: a chute writes the
    log lines it parses. This is the difference between a parsing bug in the shipper being a
    crash and being cluster-wide code execution.
    """
    outer, child = _split_shipper_rules()

    assert any(
        rule == "/usr/local/bin/crictl-pods-helper cx -> crictl," for rule in outer
    ), "the wrapper must transition to the child, not inherit via rix/px"

    for path in (
        "/run/k3s/containerd/containerd.sock",
        "/usr/local/bin/k3s",
        "/var/lib/rancher/k3s/data/*/bin/k3s",
    ):
        assert not any(
            rule.startswith(f"{path} ") for rule in outer
        ), f"{path} is reachable from the parent profile"
        assert any(
            rule.startswith(f"{path} ") for rule in child
        ), f"{path} is missing from the crictl child"


def test_log_shipper_cannot_exec_a_shell_outside_the_crictl_child():
    """No shell in the parent profile.

    /bin/bash was granted at top level so the wrapper script could run. With the wrapper
    behind `cx ->` the shell belongs in the child; leaving it in the parent hands a shell to
    the long-running process for free, which is the first thing an exploit wants.
    """
    outer, child = _split_shipper_rules()
    for shell in (
        "/bin/bash",
        "/bin/sh",
        "/bin/dash",
        "/usr/bin/bash",
        "/usr/bin/dash",
    ):
        assert not any(
            rule.startswith(f"{shell} ") for rule in outer
        ), f"{shell} is executable from the parent profile; it belongs in the child"
    assert any(rule.startswith("/usr/bin/dash ") for rule in child)
    assert not any(
        rule.startswith(("/bin/bash ", "/usr/bin/bash ")) for rule in child
    ), "bash in the crictl child re-opens the $BASH_ENV path into the socket"


def test_crictl_wrapper_is_not_bash():
    """The wrapper must be POSIX sh, because it is the door into the socket profile.

    bash sources $BASH_ENV before running the script body -- verified by execution -- so
    with a bash wrapper a caller that already had code execution in the parent profile
    could set BASH_ENV and have its payload run inside the crictl child, which holds the
    CRI socket. That defeats the allowlist entirely. dash reads neither $BASH_ENV nor,
    non-interactively, $ENV.
    """
    wrapper = REPO / "ansible/guest/roles/chute-log-shipper/files/crictl-pods-helper"
    first = wrapper.read_text().splitlines()[0]
    assert first == "#!/bin/sh", f"wrapper shebang is {first!r}, not #!/bin/sh"
    assert (
        "pipefail" not in wrapper.read_text()
    ), "set -o pipefail is not POSIX; dash rejects it"


def test_every_run_chutes_consumer_declares_its_own_subtree():
    """With /run/chutes at 0700, reaching into it must be declared, not inherited.

    A bind at the SAME path does not grant reach: resolution still walks the real 0700
    /run/chutes before crossing the mount (verified by execution -- a bind under a parent
    with no search bit is unreadable, and restoring only o+x makes it readable). So each
    consumer mounts a private tmpfs over /run/chutes and binds its subtree into that.
    Dropping the TemporaryFileSystem= while keeping the bind looks harmless and silently
    breaks the service at boot, which is what this pins.

    system-manager is deliberately absent: it references /run/chutes only through
    EnvironmentFile=, which PID 1 reads as root before dropping to User=.
    """
    roles = REPO / "ansible/guest/roles"
    units = {
        path.name: path.read_text()
        for path in list(roles.glob("*/files/*.service"))
        + list(roles.glob("*/files/*.conf"))
    }

    binders = {
        name: [
            line.split("=-", 1)[1].strip()
            for line in text.splitlines()
            if line.startswith("BindReadOnlyPaths=-/run/chutes")
        ]
        for name, text in units.items()
        if "BindReadOnlyPaths=-/run/chutes" in text
    }
    assert binders == {
        "admission-controller-signing-keys.conf": ["/run/chutes/signing-keys"],
        "admission-registry-tls.conf": ["/run/chutes/registry-tls"],
        "attestation-service.service": ["/run/chutes/proxy-tls"],
        "chute-log-shipper.conf": ["/run/chutes/registry-tls"],
    }, "the set of units binding under /run/chutes changed"

    for name in binders:
        # A drop-in may rely on the tmpfs declared in the unit it drops into.
        owner = "admission-controller.service" if name.startswith("admission") else name
        assert "TemporaryFileSystem=/run/chutes:ro,mode=0755" in units[owner], (
            f"{name}: binds under /run/chutes without an explicit-mode TemporaryFileSystem= "
            f"(declared in {owner}); the bind alone cannot cross the 0700 parent"
        )


def test_system_manager_reads_nothing_from_run_chutes():
    """system-manager holds no /run/chutes grant, because it opens nothing there.

    ALLOWED_VALIDATORS arrives via EnvironmentFile=/run/chutes/validator-auth.env, which
    PID 1 reads as root before dropping to User=system-manager. The cosign key paths are
    fields on AdmissionConfig, and nothing under sek8s/system_manager/ references a public
    key or a signature. Both grants were dead; re-adding one would also make this service
    a fourth consumer needing a bind under the 0700 parent.
    """
    profile = (PROFILE_DIR / "sek8s.system-manager").read_text()
    rules = [
        line.strip()
        for line in profile.splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    grants = [
        r for r in rules if r.startswith("/run/chutes") and not r.startswith("deny")
    ]
    assert (
        not grants
    ), f"system-manager grants /run/chutes paths it does not read: {grants}"
