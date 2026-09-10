"""Tests for the systemd watchdog keepalive in k3s-post-start.sh.

The wrapper runs each cluster-init step under `timeout MAX_SCRIPT_TIMEOUT`. It used
to block on that call with no keepalives, so the quiet window equalled the step's
runtime: any step slower than WatchdogSec (120s at the time) had the whole unit
SIGABRT'd mid-run, and since the step never finished, its completion marker was
never written and the restart replayed the same kill forever. These tests pin the
fix — pings continue *while* a step runs — and the exit-code plumbing around it.
"""

from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SCRIPT = "ansible/guest/roles/k3s/files/k3s-post-start.sh"
UNIT = REPO / "ansible/guest/roles/k3s/templates/k3s-post-start.service.j2"


def _prepare(shell, step_body, *, step_name="50-step.sh"):
    """Stub out everything the wrapper shells to, and install one step script."""
    script_dir = shell.tmp / "init-scripts"
    script_dir.mkdir()
    step = script_dir / step_name
    step.write_text("#!/usr/bin/env bash\n" + step_body)
    step.chmod(0o755)  # get_script_list only picks up -executable files

    notify_log = shell.tmp / "notify.log"
    # Record every notification with a timestamp so we can tell pings that happened
    # *during* the step from the ones bracketing it.
    shell.stub(
        "systemd-notify",
        f'printf "%s %s\\n" "$(date +%s)" "$*" >> "{notify_log}"\n',
    )
    shell.stub("systemctl", "exit 0\n")
    shell.stub("kubectl", "exit 0\n")
    shell.stub("poweroff", "exit 0\n")

    env = {
        "SCRIPT_DIR": str(script_dir),
        "MARKER_DIR": str(shell.tmp / "markers"),
        "LOG_FILE": str(shell.tmp / "post-start.log"),
        "NOTIFY_SOCKET": "/run/systemd/notify",
        "WATCHDOG_PING_INTERVAL": "1",
        "MAX_SCRIPT_TIMEOUT": "20",
    }
    return env, notify_log


def _watchdog_pings(notify_log):
    if not notify_log.exists():
        return []
    return [
        line for line in notify_log.read_text().splitlines() if "WATCHDOG=1" in line
    ]


def test_watchdog_pinged_while_step_runs(shell):
    """A step slower than the ping interval must be pinged through, not just around."""
    env, notify_log = _prepare(shell, "sleep 5\nexit 0\n")

    result = shell.run(SCRIPT, env=env)

    assert result.returncode == 0, result.stdout + result.stderr
    pings = _watchdog_pings(notify_log)
    # Two pings bracket every step; a 5s step at a 1s interval must add several more.
    assert len(pings) > 4, f"expected keepalives during the step, got {pings}"

    # And they must be spread across the step, not bunched at its edges.
    stamps = sorted(int(line.split()[0]) for line in pings)
    assert stamps[-1] - stamps[0] >= 3, f"pings did not span the step: {stamps}"


def test_step_failure_still_reported(shell):
    """Backgrounding the step must not swallow its exit code."""
    env, notify_log = _prepare(shell, "exit 3\n")

    result = shell.run(SCRIPT, env=env)

    assert "failed with exit code 3" in result.stdout, result.stdout
    marker = shell.tmp / "markers" / "50-step.sh.failed"
    assert marker.exists()
    assert "exit_code=3" in marker.read_text()


def test_step_timeout_still_detected(shell):
    """`timeout` still bounds a step; the ping loop must not mask a hang."""
    env, notify_log = _prepare(shell, "sleep 30\n")
    env["MAX_SCRIPT_TIMEOUT"] = "2"

    result = shell.run(SCRIPT, env=env)

    assert "timed out after 2s" in result.stdout, result.stdout


def test_unit_watchdog_exceeds_script_timeout():
    """WatchdogSec must stay above the per-step timeout the wrapper allows."""
    unit = UNIT.read_text()
    watchdog = int(
        next(
            line for line in unit.splitlines() if line.startswith("WatchdogSec=")
        ).split("=")[1]
    )
    wrapper = (REPO / SCRIPT).read_text()
    max_timeout = int(
        next(
            line
            for line in wrapper.splitlines()
            if line.startswith("MAX_SCRIPT_TIMEOUT=")
        )
        .split("-")[1]
        .split("}")[0]
    )
    assert watchdog > max_timeout, (
        f"WatchdogSec={watchdog} <= MAX_SCRIPT_TIMEOUT={max_timeout}: a slow step "
        "would be killed mid-run and the unit would restart-loop"
    )


def test_unit_start_limits_are_in_unit_section():
    """StartLimit* are ignored in [Service] on modern systemd — keep them in [Unit]."""
    # Split on the section header line itself — prose in a comment may mention
    # "[Service]" without starting the section.
    unit_section, service_section = UNIT.read_text().split("\n[Service]\n", 1)
    assert "StartLimitIntervalSec=" in unit_section
    assert "StartLimitBurst=" in unit_section
    directives = [
        line for line in service_section.splitlines() if not line.startswith("#")
    ]
    assert not any(line.startswith("StartLimit") for line in directives)


def test_staged_secrets_are_root_only():
    """Staging must not widen a secret's mode for the life of the consuming script.

    The sources are 0600 root inside a 0700 /run/chutes. Staging them 0444 in a 0755
    directory made them world-readable to anything already running on the guest, and the
    window is not short — the step consuming the first one rewrites every secret and
    configmap through the apiserver. Every step runs as root under aa-exec from a
    User=root unit, so root-only staging costs nothing.

    Asserted against the source rather than by running it: the staged-file map hardcodes
    /run/chutes paths a test cannot create without root, so exercising the real path would
    need a privileged harness. This at least fails loudly if the modes are widened again.
    """
    body = (REPO / SCRIPT).read_text()
    start = body.index("stage_script_files()")
    end = body.index("unstage_script_files()")
    staging = body[start:end]
    # Commands only — the comment above them names the old modes to explain why they
    # were wrong, and matching raw text would flag that as a regression.
    commands = [
        ln.strip()
        for ln in staging.splitlines()
        if ln.strip() and not ln.strip().startswith("#")
    ]
    modes = [ln for ln in commands if "chmod " in ln or "install " in ln]

    assert any(
        "chmod 0700" in ln for ln in modes
    ), "staging tree must not be world-traversable"
    assert any(
        "install -m 0400" in ln for ln in modes
    ), "staged secrets must be root-read-only"
    assert not any(
        "0755" in ln or "0444" in ln for ln in modes
    ), f"staging reverted to world-readable modes: {modes}"
