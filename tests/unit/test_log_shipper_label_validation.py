"""Pod-label values must be validated before they reach an egress URL.

`config_id` comes from a miner-authored pod label and is concatenated into the path the
shipper POSTs to, carrying the VM's mTLS client identity. A `/` in that value is not
escaped — it is structural by the time the URL is parsed — so control of the label would
be control of which endpoint the guest's attested identity is spent on.

The apiserver's label grammar is what makes that unreachable, and it is the ONLY thing:
the often-assumed second barrier, that the HTTP client percent-encodes the path, does not
apply to a value already concatenated into the URL string. The first test below records
that, so nobody re-derives the wrong conclusion.
"""

import json

import pytest
from yarl import URL

from sek8s.log_shipper.config import LogShipperConfig
from sek8s.log_shipper.crictl import _is_label_value, parse_chute_pods


@pytest.fixture
def config(tmp_path) -> LogShipperConfig:
    return LogShipperConfig(
        POD_LOG_ROOT=str(tmp_path),
        CHECKPOINT_PATH=str(tmp_path / "ckpt.json"),
    )


def test_url_assembly_does_not_neutralise_a_traversing_value():
    """The reason validation is required: the URL layer does not save us.

    Percent-encoding protects a value passed as a URL *component*. This one is
    interpolated into the string first, so the slashes are structural and `..` is
    resolved away — the request leaves the intended prefix entirely.
    """
    base = "https://cvm.chutes.ai/instances/launch_config"
    assert str(URL(f"{base}/abc123/logs")).endswith(
        "/instances/launch_config/abc123/logs"
    )

    escaped = str(URL(f"{base}/../../admin/keys/logs"))
    assert escaped == "https://cvm.chutes.ai/admin/keys/logs"
    assert "launch_config" not in escaped


@pytest.mark.parametrize(
    "value",
    [
        "abc123",
        "a",
        "chute-1.2_3",
        "A" * 63,
    ],
)
def test_valid_label_values_are_accepted(value):
    """Anything the apiserver would accept must still pass, or this rejects real pods."""
    assert _is_label_value(value)


@pytest.mark.parametrize(
    "value",
    [
        "../../admin/keys",
        "a/b",
        "x%2f..%2fadmin",
        "a\nInjected: line",
        "with space",
        "-leading-dash",
        "trailing-dash-",
        ".leading-dot",
        "A" * 64,
        "",
    ],
)
def test_values_outside_the_label_grammar_are_rejected(value):
    assert not _is_label_value(value)


def _pod(config_id, deployment_id="dep-1"):
    return {
        "metadata": {"name": "chute-pod", "uid": "uid-1", "namespace": "chutes"},
        "labels": {
            "chutes/chute": "true",
            "chutes/config-id": config_id,
            "chutes/deployment-id": deployment_id,
        },
        "state": "SANDBOX_READY",
    }


def test_a_pod_with_a_traversing_config_id_is_skipped(config):
    """The pod is dropped rather than shipped to an attacker-chosen path.

    Skipping matches the module's existing posture for metadata it cannot use, and
    means one malformed pod cannot stop capture for its healthy siblings.
    """
    raw = json.dumps({"items": [_pod("../../admin/keys"), _pod("good-config-1")]})
    assert [p.config_id for p in parse_chute_pods(raw, config)] == ["good-config-1"]


def test_a_pod_with_a_bad_deployment_id_is_skipped(config):
    """deployment_id ships in the JSON body rather than the path, so it cannot traverse
    — but it is the same miner-authored input and gets the same treatment."""
    raw = json.dumps({"items": [_pod("good-config-1", deployment_id="a/b")]})
    assert parse_chute_pods(raw, config) == []
