"""Tests for the launch config model (chutes_cvm.guest.config.LaunchConfig).

Covers precedence (CLI > env > config.yaml > defaults) with nested per-area sections that mirror
the config.yaml, cross-source deep-merge, removed-key errors, and template generation.
"""

import pytest
import yaml
from chutes_cvm.guest import config as cfgmod
from chutes_cvm.guest.config import ConfigError, LaunchConfig, render_config_template

_YAML = {
    "vm": {"hostname": "yaml-host"},
    "network": {"vm_ip": "10.0.0.5", "type": "user"},
    "volumes": {"cache": {"size": "9000G"}},
    "devices": {"bind_devices": False},
}


def _write(tmp_path, data) -> str:
    p = tmp_path / "config.yaml"
    p.write_text(yaml.safe_dump(data))
    return str(p)


def test_defaults_when_no_sources():
    cfg = LaunchConfig.from_file(None)
    assert cfg.vm.hostname == ""
    assert cfg.network.vm_ip == "192.168.100.2"
    assert cfg.network.type == "tap"
    assert cfg.devices.bind_devices is True
    assert cfg.network.ssh_port == 2222
    assert cfg.volumes.cache.size == "5000G"


def test_nested_yaml_loads_natively(tmp_path):
    cfg = LaunchConfig.from_file(_write(tmp_path, _YAML))
    assert cfg.vm.hostname == "yaml-host"
    assert cfg.network.vm_ip == "10.0.0.5"
    assert cfg.network.type == "user"
    assert cfg.volumes.cache.size == "9000G"
    assert cfg.devices.bind_devices is False


def test_env_overrides_yaml_and_deep_merges(tmp_path, monkeypatch):
    # env sets one network leaf; the YAML's other network leaf must survive (deep merge).
    monkeypatch.setenv("CHUTES_CVM_NETWORK__BRIDGE_IP", "172.16.0.1/24")
    cfg = LaunchConfig.from_file(_write(tmp_path, _YAML))
    assert cfg.network.bridge_ip == "172.16.0.1/24"  # env
    assert cfg.network.vm_ip == "10.0.0.5"  # YAML still applies


def test_cli_overrides_env_and_yaml(tmp_path, monkeypatch):
    monkeypatch.setenv("CHUTES_CVM_NETWORK__VM_IP", "172.16.0.9")
    cfg = LaunchConfig.from_file(_write(tmp_path, _YAML), network={"vm_ip": "1.2.3.4"})
    assert cfg.network.vm_ip == "1.2.3.4"  # CLI (init) beats env and YAML


def test_yaml_loads_into_nested_model(tmp_path):
    cfg = LaunchConfig.from_file(_write(tmp_path, _YAML))
    assert cfg.vm.hostname == "yaml-host"
    assert cfg.network.vm_ip == "10.0.0.5"
    assert cfg.volumes.cache.size == "9000G"
    assert cfg.devices.bind_devices is False


def test_missing_config_file_raises():
    with pytest.raises(ConfigError, match="not found"):
        LaunchConfig.from_file("/no/such/config.yaml")


def test_removed_advanced_section_raises(tmp_path):
    with pytest.raises(ConfigError, match="advanced"):
        LaunchConfig.from_file(_write(tmp_path, {"advanced": {"x": 1}}))


def test_removed_cache_enabled_raises(tmp_path):
    with pytest.raises(ConfigError, match="cache.enabled"):
        LaunchConfig.from_file(
            _write(tmp_path, {"volumes": {"cache": {"enabled": True}}})
        )


def test_bad_network_type_raises(tmp_path):
    with pytest.raises(ConfigError):
        LaunchConfig.from_file(_write(tmp_path, {"network": {"type": "bogus"}}))


def test_template_is_valid_and_roundtrips(tmp_path):
    text = render_config_template()
    doc = yaml.safe_load(text)
    # Nested structure the miner edits, straight from the model.
    assert doc["vm"]["hostname"] == ""
    assert doc["network"]["type"] == "tap"
    assert doc["volumes"]["cache"]["size"] == "5000G"
    # The generated file must load back through the model without error.
    cfg = LaunchConfig.from_file(_write(tmp_path, doc))
    assert cfg.network.type == "tap"


def test_config_verify_command(tmp_path, capsys):
    ok = _write(tmp_path, _YAML)
    assert cfgmod.main(["verify", ok]) == 0
    assert "valid" in capsys.readouterr().out
    assert cfgmod.main(["verify", "/no/such.yaml"]) == 1


def test_config_init_command_writes_file(tmp_path):
    out = tmp_path / "generated.yaml"
    assert cfgmod.main(["init", "--output", str(out)]) == 0
    cfg = LaunchConfig.from_file(str(out))
    assert cfg.network.type == "tap"
    # Refuses to overwrite without --force.
    assert cfgmod.main(["init", "--output", str(out)]) == 1
