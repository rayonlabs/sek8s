import importlib

import pytest
from fastapi.testclient import TestClient

from sek8s.system_manager.status.models import SERVICE_ALLOWLIST, CommandResult


class FakeRunner:
    def __init__(self):
        self.commands = []
        self.responses: dict[str, CommandResult] = {}

    def set_response(self, binary: str, result: CommandResult) -> None:
        self.responses[binary] = result

    async def __call__(
        self, command, timeout, limit, *, keep_tail=False
    ):  # pragma: no cover - interface shim
        self.commands.append(command)
        binary = command[0]
        if binary not in self.responses:
            raise AssertionError(f"No response registered for {binary}")
        return self.responses[binary]


@pytest.fixture
def fake_runner(monkeypatch):
    runner = FakeRunner()
    util_mod = importlib.import_module("sek8s.system_manager.status.util")
    router_mod = importlib.import_module("sek8s.system_manager.status.router")
    monkeypatch.setattr(util_mod, "run_command", runner)
    monkeypatch.setattr(router_mod, "run_command", runner)
    return runner


@pytest.fixture
def status_client(manager_app_no_auth):
    """Test client for status endpoints (auth bypassed via manager_app_no_auth)."""
    with TestClient(manager_app_no_auth) as client:
        yield client


def test_list_services(status_client):
    response = status_client.get("/status/services")
    assert response.status_code == 200
    data = response.json()
    service_ids = {svc["id"] for svc in data["services"]}
    expected = {
        "admission-controller",
        "attestation-service",
        "chute-log-shipper",
        "k3s",
        "nvidia-persistenced",
        "nvidia-fabricmanager",
        "opa",
        "system-manager",
    }
    assert expected.issubset(service_ids)


def test_allowlist_units_match_service_ids():
    """Every allowlist key must map to the unit the guest image actually installs."""
    expected_units = {
        "chute-log-shipper": "chute-log-shipper.service",
        "opa": "opa.service",
    }
    for service_id, unit in expected_units.items():
        assert SERVICE_ALLOWLIST[service_id].unit == unit
    # Keys and service_id fields must not drift apart — the id is the API path segment.
    for service_id, definition in SERVICE_ALLOWLIST.items():
        assert definition.service_id == service_id


def test_service_status_parsing(status_client, fake_runner):
    fake_runner.set_response(
        "systemctl",
        CommandResult(
            exit_code=0,
            stdout=(
                "Id=admission-controller.service\n"
                "LoadState=loaded\n"
                "ActiveState=active\n"
                "SubState=running\n"
                "MainPID=1234\n"
                "ExecMainStatus=0\n"
                "ExecMainCode=0\n"
                "UnitFileState=enabled\n"
            ),
            stderr="",
            stdout_truncated=False,
            stderr_truncated=False,
        ),
    )

    response = status_client.get("/status/services/admission-controller/status")
    assert response.status_code == 200
    data = response.json()
    assert data["status"]["active_state"] == "active"
    assert data["status"]["main_pid"] == "1234"
    assert data["healthy"] is True
    assert fake_runner.commands[-1][0] == "systemctl"


def test_logs_endpoint_respects_clamp(status_client, fake_runner):
    fake_runner.set_response(
        "journalctl",
        CommandResult(
            exit_code=0,
            stdout="line1\nline2\n",
            stderr="",
            stdout_truncated=False,
            stderr_truncated=False,
        ),
    )

    response = status_client.get("/status/services/k3s/logs?lines=5001")
    assert response.status_code == 200
    data = response.json()
    assert data["returned_lines"] == 2
    assert any("--lines=1000" in arg for arg in fake_runner.commands[-1])


def test_nvidia_smi_command_building(status_client, fake_runner):
    fake_runner.set_response(
        "nvidia-smi",
        CommandResult(
            exit_code=0,
            stdout="gpu output\nsecond line",
            stderr="",
            stdout_truncated=False,
            stderr_truncated=False,
        ),
    )

    response = status_client.get("/status/gpu/nvidia-smi?detail=true&gpu=0")
    assert response.status_code == 200
    data = response.json()
    assert data["command"] == ["nvidia-smi", "-q", "-i", "0"]
    assert fake_runner.commands[-1] == ["nvidia-smi", "-q", "-i", "0"]
    assert data["stdout_lines"] == ["gpu output", "second line"]


def test_unknown_service_returns_404(status_client):
    response = status_client.get("/status/services/unknown/status")
    assert response.status_code == 404


def test_overview_success(status_client, fake_runner):
    fake_runner.set_response(
        "systemctl",
        CommandResult(
            exit_code=0,
            stdout=(
                "Id=admission-controller.service\n"
                "LoadState=loaded\n"
                "ActiveState=active\n"
                "SubState=running\n"
                "MainPID=1234\n"
                "ExecMainStatus=0\n"
                "ExecMainCode=0\n"
                "UnitFileState=enabled\n"
            ),
            stderr="",
            stdout_truncated=False,
            stderr_truncated=False,
        ),
    )
    fake_runner.set_response(
        "nvidia-smi",
        CommandResult(
            exit_code=0,
            stdout="gpu output",
            stderr="",
            stdout_truncated=False,
            stderr_truncated=False,
        ),
    )

    response = status_client.get("/status/overview")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "ok"
    assert len(data["services"]) == len(SERVICE_ALLOWLIST)
    assert all(entry["healthy"] for entry in data["services"])
    assert data["gpu"]["status"] == "ok"


def test_overview_ok_with_exited_oneshot_services(status_client, fake_runner):
    """Oneshot services report active/exited after success — overview must be ok."""
    fake_runner.set_response(
        "systemctl",
        CommandResult(
            exit_code=0,
            stdout=(
                "Id=setup-storage-bind-mounts.service\n"
                "LoadState=loaded\n"
                "ActiveState=active\n"
                "SubState=exited\n"
                "MainPID=0\n"
                "ExecMainStatus=0\n"
                "ExecMainCode=1\n"
                "UnitFileState=enabled\n"
            ),
            stderr="",
            stdout_truncated=False,
            stderr_truncated=False,
        ),
    )
    fake_runner.set_response(
        "nvidia-smi",
        CommandResult(
            exit_code=0,
            stdout="gpu output",
            stderr="",
            stdout_truncated=False,
            stderr_truncated=False,
        ),
    )

    response = status_client.get("/status/overview")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "ok"
    assert all(entry["healthy"] for entry in data["services"])


def test_oneshot_service_healthy_when_exited_zero(status_client, fake_runner):
    fake_runner.set_response(
        "systemctl",
        CommandResult(
            exit_code=0,
            stdout=(
                "Id=setup-storage-bind-mounts.service\n"
                "LoadState=loaded\n"
                "ActiveState=active\n"
                "SubState=exited\n"
                "MainPID=0\n"
                "ExecMainStatus=0\n"
                "ExecMainCode=1\n"
                "UnitFileState=enabled\n"
            ),
            stderr="",
            stdout_truncated=False,
            stderr_truncated=False,
        ),
    )

    response = status_client.get("/status/services/storage-bind-mounts/status")
    assert response.status_code == 200
    data = response.json()
    assert data["status"]["sub_state"] == "exited"
    assert data["status"]["exit_status"] == "0"
    assert data["healthy"] is True


def test_oneshot_service_unhealthy_when_exited_nonzero(status_client, fake_runner):
    fake_runner.set_response(
        "systemctl",
        CommandResult(
            exit_code=0,
            stdout=(
                "Id=setup-storage-bind-mounts.service\n"
                "LoadState=loaded\n"
                "ActiveState=active\n"
                "SubState=exited\n"
                "MainPID=0\n"
                "ExecMainStatus=1\n"
                "ExecMainCode=1\n"
                "UnitFileState=enabled\n"
            ),
            stderr="",
            stdout_truncated=False,
            stderr_truncated=False,
        ),
    )

    response = status_client.get("/status/services/storage-bind-mounts/status")
    assert response.status_code == 200
    data = response.json()
    assert data["status"]["sub_state"] == "exited"
    assert data["status"]["exit_status"] == "1"
    assert data["healthy"] is False


def test_fabricmanager_healthy_when_masked(status_client, fake_runner):
    """Masked nvidia-fabricmanager must be reported healthy (valid on non-NVLink hosts)."""
    fake_runner.set_response(
        "systemctl",
        CommandResult(
            exit_code=0,
            stdout=(
                "Id=nvidia-fabricmanager.service\n"
                "LoadState=masked\n"
                "ActiveState=inactive\n"
                "SubState=dead\n"
                "MainPID=0\n"
                "ExecMainStatus=0\n"
                "ExecMainCode=0\n"
                "UnitFileState=masked\n"
            ),
            stderr="",
            stdout_truncated=False,
            stderr_truncated=False,
        ),
    )

    response = status_client.get("/status/services/nvidia-fabricmanager/status")
    assert response.status_code == 200
    data = response.json()
    assert data["status"]["unit_file_state"] == "masked"
    assert data["healthy"] is True


def test_is_service_healthy_masked_with_masked_ok():
    """is_service_healthy returns True for a masked service when masked_ok=True."""
    from sek8s.system_manager.status.responses import ServiceStatus
    from sek8s.system_manager.status.util import is_service_healthy

    status = ServiceStatus(
        load_state="masked",
        active_state="inactive",
        sub_state="dead",
        unit_file_state="masked",
        main_pid="0",
        exit_code="0",
        exit_status="0",
    )
    assert is_service_healthy(status, masked_ok=True) is True


def test_is_service_healthy_masked_without_masked_ok():
    """is_service_healthy returns False for a masked service when masked_ok=False."""
    from sek8s.system_manager.status.responses import ServiceStatus
    from sek8s.system_manager.status.util import is_service_healthy

    status = ServiceStatus(
        load_state="masked",
        active_state="inactive",
        sub_state="dead",
        unit_file_state="masked",
        main_pid="0",
        exit_code="0",
        exit_status="0",
    )
    assert is_service_healthy(status, masked_ok=False) is False


def test_fabricmanager_has_masked_ok_set():
    """nvidia-fabricmanager ServiceDefinition must have masked_ok=True."""
    from sek8s.system_manager.status.models import SERVICE_ALLOWLIST

    assert SERVICE_ALLOWLIST["nvidia-fabricmanager"].masked_ok is True


def test_non_masked_ok_service_unhealthy_when_masked(status_client, fake_runner):
    """A service without masked_ok=True must be reported unhealthy when masked."""
    fake_runner.set_response(
        "systemctl",
        CommandResult(
            exit_code=0,
            stdout=(
                "Id=k3s.service\n"
                "LoadState=masked\n"
                "ActiveState=inactive\n"
                "SubState=dead\n"
                "MainPID=0\n"
                "ExecMainStatus=0\n"
                "ExecMainCode=0\n"
                "UnitFileState=masked\n"
            ),
            stderr="",
            stdout_truncated=False,
            stderr_truncated=False,
        ),
    )

    response = status_client.get("/status/services/k3s/status")
    assert response.status_code == 200
    data = response.json()
    assert data["status"]["unit_file_state"] == "masked"
    assert data["healthy"] is False


def test_overview_degraded_on_service_failure(status_client, fake_runner):
    fake_runner.set_response(
        "systemctl",
        CommandResult(
            exit_code=2,
            stdout="",
            stderr="boom",
            stdout_truncated=False,
            stderr_truncated=False,
        ),
    )
    fake_runner.set_response(
        "nvidia-smi",
        CommandResult(
            exit_code=0,
            stdout="gpu output",
            stderr="",
            stdout_truncated=False,
            stderr_truncated=False,
        ),
    )

    response = status_client.get("/status/overview")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "degraded"
    assert any(entry.get("error") for entry in data["services"])
