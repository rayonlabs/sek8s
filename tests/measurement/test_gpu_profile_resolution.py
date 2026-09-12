"""The measurement path must not resolve a GPU profile more loosely than the launch path.

What gets published as a host class's `expected_gpus` is decided here, and the control
plane later enforces it. The launch path has always refused to guess — it rejects mixed or
unsupported models outright — so this side resolving on a first partial match meant one
high-value device id beside anything else published that profile for the whole class, and
an honest heterogeneous host was measured as the wrong SKU.
"""

import pytest
from chutes_cvm.guest.gpu.profiles import GPU_PROFILES
from chutes_cvm.measurement.generate_measurements import _resolve_profile_for_devices


def device_id(profile_name: str) -> str:
    return GPU_PROFILES[profile_name].pci_device_id


@pytest.mark.parametrize("name", sorted(GPU_PROFILES))
def test_homogeneous_devices_resolve_to_their_profile(name):
    assert _resolve_profile_for_devices([device_id(name)] * 8).name == name


def test_mixed_models_are_rejected_not_resolved_to_the_first():
    """The defect: insertion order decided the answer rather than the evidence."""
    ids = [device_id("B200"), device_id("H200")]
    with pytest.raises(ValueError, match="must be one model"):
        _resolve_profile_for_devices(ids)


def test_mixed_is_rejected_regardless_of_order():
    """Order must not matter — otherwise the check is really a first-match in disguise."""
    for ids in (
        [device_id("H200"), device_id("B200")],
        [device_id("B200"), device_id("H200")],
    ):
        with pytest.raises(ValueError, match="must be one model"):
            _resolve_profile_for_devices(ids)


def test_a_known_id_beside_an_unknown_one_is_rejected():
    """It must not resolve to the known profile — that is the first-match bug again."""
    with pytest.raises(ValueError, match="must be one model"):
        _resolve_profile_for_devices([device_id("H200"), "dead"])


def test_an_unrecognised_id_is_rejected():
    with pytest.raises(ValueError, match="no GPU profile matches"):
        _resolve_profile_for_devices(["dead"] * 8)


def test_an_unsupported_edition_does_not_resolve_to_its_sibling():
    """2bb1 (RTX PRO 6000 Workstation) is a different product and is not supported.

    It used to share RTX_PRO_6000 with 2bb5 (Server), so a Workstation host was measured
    with the BAR layout observed on a Server card.
    """
    with pytest.raises(ValueError, match="no GPU profile matches"):
        _resolve_profile_for_devices(["2bb1"] * 8)


def test_empty_is_rejected():
    with pytest.raises(ValueError):
        _resolve_profile_for_devices([])


def test_measurement_path_is_no_looser_than_the_launch_path():
    """Both sides must reject a heterogeneous host; only the wording differs.

    The launch path resolves from detected model names, this one from PCI ids, so they
    cannot share an implementation — but they must not disagree about what is valid.
    """
    from chutes_cvm.guest.gpu.profiles import resolve_profile

    with pytest.raises(ValueError, match="Mixed GPU"):
        resolve_profile({"0": "B200", "1": "H200"})
    with pytest.raises(ValueError, match="must be one model"):
        _resolve_profile_for_devices([device_id("B200"), device_id("H200")])
