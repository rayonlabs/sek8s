"""Tests for the host-readiness verify entrypoint (chutes_cvm.guest.verify).

verify_host is API-backed: Gate A is the local QEMU check; Gate B asks
POST /servers/tdx/host_profiles/status (run_host_class_status) whether this host CLASS is known and
which published images cover it. Gate B is version-free — a host is verified before it has
downloaded any image — so a missing image or manifest must never block it. A downloaded image is
only checked against the covered set as a note. Any status-call failure fails closed to BLOCKED;
``--submit`` registers an unknown class.
"""

from contextlib import ExitStack
from unittest.mock import patch

from chutes_cvm.guest import verify
from chutes_cvm.guest.preflight import PreflightError

COVERED = [{"version": "1.4.0", "rc": False}, {"version": "1.4.0", "rc": True}]


def _patch(
    covered=None,
    status="accepted",
    qemu_raises=False,
    status_raises=False,
    local_image=None,
):
    """Patch the QEMU gate, the host-class status call, and the local-image note lookup.

    ``local_image`` is the (version, rc) a downloaded manifest would report; None means nothing is
    downloaded (the normal case for a freshly verified host). Returns (ExitStack, gate, status).
    """
    stack = ExitStack()
    gate = stack.enter_context(
        patch("chutes_cvm.guest.verify.verify_host_qemu_supported")
    )
    if qemu_raises:
        gate.side_effect = ValueError("qemu 10.1.0 != expected 10.2.1")
    img = stack.enter_context(patch("chutes_cvm.guest.verify._image_version_rc"))
    if local_image is None:
        img.side_effect = FileNotFoundError("no manifest")
    else:
        img.return_value = local_image
    st = stack.enter_context(patch("chutes_cvm.guest.verify.run_host_class_status"))
    if status_raises:
        st.side_effect = PreflightError("API unreachable")
    else:
        st.return_value = {
            "fingerprint": "fp",
            "status": status,
            "measurements": COVERED if covered is None else covered,
            "detail": "d",
        }
    return stack, gate, st


def test_ready_when_the_class_is_measured(capsys):
    stack, _, _ = _patch()
    with stack:
        assert verify.verify_host(scripts_dir="/x") == verify.READY
    out = capsys.readouterr().out
    assert "1.4.0" in out and "1.4.0 (rc)" in out


def test_no_downloaded_image_does_not_block():
    """The regression this endpoint exists for: a host with nothing downloaded still verifies."""
    stack, _, st = _patch(local_image=None)
    with stack:
        assert verify.verify_host(scripts_dir="/x") == verify.READY
        st.assert_called_once()


def test_status_call_is_version_free():
    """No version may reach the API — that dependency is what forced an image to be present."""
    stack, _, st = _patch()
    with stack:
        verify.verify_host(scripts_dir="/x")
    assert "version" not in st.call_args.kwargs
    assert "rc" not in st.call_args.kwargs


def test_warning_when_nothing_is_published_for_the_class(capsys):
    stack, _, _ = _patch(covered=[], status="unknown")
    with stack:
        assert verify.verify_host(scripts_dir="/x") == verify.WARNING
    assert "submit-profile" in capsys.readouterr().out


def test_pending_class_is_told_to_wait_not_resubmit(capsys):
    """Already registered: re-submitting neither helps nor advances the queue."""
    stack, _, _ = _patch(covered=[], status="pending")
    with stack:
        assert verify.verify_host(scripts_dir="/x") == verify.WARNING
    out = capsys.readouterr().out
    assert "Nothing to do" in out
    assert "submit-profile" not in out


def test_blocked_when_qemu_gate_fails():
    # The QEMU gate runs first (as-is mode); when it fails we never reach the API.
    stack, _, st = _patch(qemu_raises=True)
    with stack:
        assert verify.verify_host(scripts_dir="/x") == verify.BLOCKED
        st.assert_not_called()


def test_blocked_when_the_status_call_fails():
    # No verdict (transport/auth/API error) -> fail closed.
    stack, _, _ = _patch(status_raises=True)
    with stack:
        assert verify.verify_host(scripts_dir="/x") == verify.BLOCKED


def test_blocked_when_target_os_unsupported():
    stack, _, st = _patch()
    with stack:
        assert verify.verify_host(target_os="99.99", scripts_dir="/x") == verify.BLOCKED
        st.assert_not_called()  # unsupported target fails before any API call


def test_target_os_skips_live_qemu_gate_and_passes_target_os():
    # --target-os mode ignores the live QEMU (the upgrade replaces it), so even a raising
    # gate doesn't block; and the target release is what gets checked at the API (preflight
    # derives that release's QEMU from it).
    stack, gate, st = _patch(qemu_raises=True)
    with stack:
        assert verify.verify_host(target_os="26.04", scripts_dir="/x") == verify.READY
        gate.assert_not_called()
        assert st.call_args.kwargs.get("target_os") == "26.04"


def test_submit_registers_an_unknown_class():
    # `verify --submit` registers an unmeasured class via submit_profile; still WARNING.
    stack, _, _ = _patch(covered=[], status="unknown")
    with stack, patch(
        "chutes_cvm.guest.verify.submit_profile",
        return_value={"status": "pending", "stored": True, "fingerprint": "fp"},
    ) as sub:
        assert verify.verify_host(scripts_dir="/x", submit=True) == verify.WARNING
        sub.assert_called_once()


def test_submit_registers_the_target_os_class_not_the_live_one():
    """`--target-os X --submit` must register the class the host BECOMES: submitting the live
    host's OS/QEMU would baseline a (release, QEMU) pair the target release never ships.
    """
    stack, _, _ = _patch(covered=[], status="unknown", qemu_raises=True)
    with stack, patch(
        "chutes_cvm.guest.verify.submit_profile",
        return_value={"status": "pending", "stored": True, "fingerprint": "fp"},
    ) as sub:
        assert (
            verify.verify_host(target_os="26.04", scripts_dir="/x", submit=True)
            == verify.WARNING
        )
        assert sub.call_args.kwargs["target_os"] == "26.04"


def test_submit_hint_carries_the_target_os(capsys):
    stack, _, _ = _patch(covered=[], status="unknown", qemu_raises=True)
    with stack:
        verify.verify_host(target_os="26.04", scripts_dir="/x")
    assert "submit-profile --target-os 26.04" in capsys.readouterr().out


def test_downloaded_image_in_the_covered_set_is_noted(capsys):
    stack, _, _ = _patch(local_image=("1.4.0", False))
    with stack:
        assert verify.verify_host(scripts_dir="/x") == verify.READY
    assert "1.4.0 is covered" in capsys.readouterr().out


def test_downloaded_image_outside_the_covered_set_is_flagged(capsys):
    """A missing measurement for one image is flagged, not fatal — the class is still viable."""
    stack, _, _ = _patch(local_image=("1.5.0", False))
    with stack:
        assert verify.verify_host(scripts_dir="/x") == verify.WARNING
    out = capsys.readouterr().out
    assert "NOT in the covered set" in out
    assert "1.4.0" in out  # still tells the operator what it CAN run


def test_rc_must_match_for_the_local_image_note(capsys):
    """A production measurement does not cover the debug (rc) build of the same version."""
    stack, _, _ = _patch(
        covered=[{"version": "1.4.0", "rc": False}], local_image=("1.4.0", True)
    )
    with stack:
        assert verify.verify_host(scripts_dir="/x") == verify.WARNING
    assert "NOT in the covered set" in capsys.readouterr().out
