"""The guest journals are miner-readable over the status API, so loguru must
never render frame-local values into a traceback."""

import sys

import pytest
from loguru import logger
from sek8s_common.log_config import configure_logging


@pytest.fixture
def restore_logger():
    """Loguru state is global — put a default sink back for later tests."""
    yield
    logger.remove()
    logger.add(sys.__stderr__)


def test_exception_traceback_omits_local_values(capsys, restore_logger):
    configure_logging()

    def _handler():
        pod_spec = {"env": [{"name": "HF_TOKEN", "value": "s3cret-tenant-value"}]}
        try:
            raise RuntimeError("boom")
        except RuntimeError:
            logger.exception(
                "Unexpected error processing admission request {}", "uid-1"
            )
        return pod_spec

    _handler()
    captured = capsys.readouterr().err

    # The exception and a usable traceback still reach the journal: suppressing
    # values must not cost us the stack we need to diagnose a prod guest.
    assert "Unexpected error processing admission request uid-1" in captured
    assert "RuntimeError: boom" in captured
    assert "Traceback (most recent call last)" in captured
    assert "in _handler" in captured  # the frame...
    assert 'raise RuntimeError("boom")' in captured  # ...and its source line
    # ...but never the values of the frame's locals.
    assert "s3cret-tenant-value" not in captured
    assert "HF_TOKEN" not in captured


def test_debug_flag_only_changes_level(capsys, restore_logger):
    configure_logging(debug=True)
    logger.debug("debug line {}", "visible")
    assert "debug line visible" in capsys.readouterr().err


def test_building_any_web_server_installs_the_hardened_sink(monkeypatch):
    """The sink is installed by the shared base, not by each entrypoint remembering to.

    The guest journals are miner-readable over the status API, and loguru's default
    handler renders local variable values inside tracebacks. Every HTTP service builds a
    WebServer before it serves — whether it then calls run() or serve() — so this is the
    one place that cannot be skipped. Asserted behaviourally rather than by reading
    source, because the point is that construction has this effect.
    """
    from sek8s_common import server as server_module
    from sek8s_common.config import ServerConfig

    calls = []
    monkeypatch.setattr(
        server_module, "configure_logging", lambda debug: calls.append(debug)
    )

    class _Server(server_module.WebServer):
        def _setup_routes(self) -> None:
            pass

    _Server(ServerConfig(DEBUG=True))
    assert calls == [True], "constructing a WebServer must install the hardened sink"


def test_the_log_shipper_daemon_installs_it_too():
    """The one service that is not a WebServer, so the base class cannot cover it."""
    import inspect

    from sek8s.services import log_shipper

    chain = inspect.getsource(log_shipper.run) + inspect.getsource(log_shipper._serve)
    assert "configure_logging" in chain
