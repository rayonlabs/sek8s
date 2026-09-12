"""Launch configuration — a nested pydantic-settings model.

`LaunchConfig` is the one source of truth for the VM launch config: per-area sections (vm,
miner, network, volumes, devices, runtime, docker_hub, rc) that mirror the `config.yaml` miners
edit — so the existing file loads natively, with no migration. Values resolve
**CLI > env (`CHUTES_CVM_*`, nested with `__`) > config.yaml > defaults** (pydantic-settings
source ordering; nested sections deep-merge across sources). The same model validates a config and
generates a starter one:

  chutes-cvm config init            # write a starter config.yaml generated from this model
  chutes-cvm config verify <file>   # validate a config.yaml against this model
"""

from __future__ import annotations

import argparse
import os
import sys
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field, ValidationError
from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic_settings.sources import YamlConfigSettingsSource


class ConfigError(Exception):
    """A launch config could not be read/validated (message is user-facing)."""


# YAML path the source reads, set by LaunchConfig.from_file before construction. The CLI is
# single-threaded, so a module global is sufficient (and avoids threading it through pydantic).
_yaml_path: "str | None" = None


class VmSection(BaseModel):
    hostname: str = Field(
        default="", description="VM hostname (must be unique per miner hotkey)"
    )
    base_image: str = Field(
        default="",
        description="Published image-set dir; empty = /var/lib/chutes/base-images/tdx-guest/",
    )
    vm_image_directory: str = Field(
        default="", description="Per-VM image dir; empty = /var/lib/chutes/vm-images/"
    )


class MinerSection(BaseModel):
    ss58: str = Field(
        default="", description="Miner SS58 credential (required unless --benchmark)"
    )
    seed: str = Field(
        default="", description="Miner seed credential (required unless --benchmark)"
    )


class NetworkSection(BaseModel):
    vm_ip: str = Field(default="192.168.100.2", description="VM IP address")
    bridge_ip: str = Field(
        default="192.168.100.1/24", description="Bridge IP with CIDR"
    )
    dns: str = Field(default="8.8.8.8", description="VM DNS server")
    public_interface: str = Field(
        default="",
        description="Public interface; empty = auto-detect from the default route",
    )
    type: Literal["tap", "user"] = Field(
        default="tap",
        description="Network type: tap (bridged) or user (SLIRP/port forwarding)",
    )
    ssh_port: int = Field(default=2222, description="SSH port for user-mode networking")


class VolumeSpec(BaseModel):
    size: str = Field(default="", description="Volume size (K/M/G/T)")
    path: str = Field(
        default="", description="Volume path; empty = auto-generate from hostname"
    )


class ConfigVolumeSpec(BaseModel):
    path: str = Field(
        default="", description="Config volume path; empty = config-<hostname>.qcow2"
    )


class VolumesSection(BaseModel):
    cache: VolumeSpec = VolumeSpec(size="5000G")
    storage: VolumeSpec = VolumeSpec(size="500G")
    config: ConfigVolumeSpec = ConfigVolumeSpec()


class DevicesSection(BaseModel):
    bind_devices: bool = Field(
        default=True,
        description="Bind GPU/NVSwitch to vfio-pci (set false to skip binding)",
    )


class RuntimeSection(BaseModel):
    foreground: bool = Field(
        default=False, description="Run the VM in the foreground instead of daemonizing"
    )


class DockerHubSection(BaseModel):
    username: str = Field(
        default="", description="Docker Hub username (optional; use with token)"
    )
    token: str = Field(
        default="", description="Docker Hub PAT/password (optional; use with username)"
    )


class LaunchConfig(BaseSettings):
    """Resolved VM launch configuration (CLI > env > config.yaml > defaults)."""

    model_config = SettingsConfigDict(
        env_prefix="CHUTES_CVM_",
        env_nested_delimiter="__",
        extra="ignore",
        case_sensitive=False,
    )

    vm: VmSection = VmSection()
    miner: MinerSection = MinerSection()
    network: NetworkSection = NetworkSection()
    volumes: VolumesSection = VolumesSection()
    devices: DevicesSection = DevicesSection()
    runtime: RuntimeSection = RuntimeSection()
    docker_hub: DockerHubSection = DockerHubSection()

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls,
        init_settings,
        env_settings,
        dotenv_settings,
        file_secret_settings,
    ):
        # Precedence (first wins): CLI (init kwargs) > env (CHUTES_CVM_*) > config.yaml > defaults.
        sources: list = [init_settings, env_settings]
        if _yaml_path:
            sources.append(YamlConfigSettingsSource(settings_cls, yaml_file=_yaml_path))
        return tuple(sources)

    @classmethod
    def from_file(cls, config_file: "str | None" = None, **overrides) -> "LaunchConfig":
        """Build the resolved config. ``config_file`` is the YAML layer; ``overrides`` is the CLI
        layer (a possibly-nested dict of only the values the user set). Precedence is
        CLI > env > YAML > defaults. Raises ConfigError on read/validation failure."""
        global _yaml_path
        _yaml_path = os.path.abspath(config_file) if config_file else None
        try:
            if _yaml_path:
                if not os.path.exists(_yaml_path):
                    raise ConfigError(f"Config file not found: {_yaml_path}")
                try:
                    with open(_yaml_path) as f:
                        data = yaml.safe_load(f) or {}
                except yaml.YAMLError as e:
                    raise ConfigError(f"Error parsing YAML: {e}") from e
                if not isinstance(data, dict):
                    raise ConfigError("config.yaml must be a mapping at the top level")
                _check_removed_keys(data)
            return cls(**overrides)
        except ValidationError as e:
            raise ConfigError(f"invalid configuration:\n{e}") from e
        finally:
            _yaml_path = None


def _check_removed_keys(data: dict) -> None:
    """Reject config keys the current schema no longer supports, with a clear message."""
    if "advanced" in data:
        raise ConfigError(
            "'advanced' section is no longer supported. Remove it to match the current schema."
        )
    if "enabled" in (data.get("volumes", {}) or {}).get("cache", {}):
        raise ConfigError(
            "'volumes.cache.enabled' has been removed. Delete it from your config."
        )


# ── Template generation (config.yaml from the schema) ────────────────────────────


def _yaml_scalar(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if value == "":
        return '""'
    return f'"{value}"'


def _emit_section(
    instance: Any, model_cls: "type[BaseModel]", indent: int
) -> list[str]:
    """Render a model's fields as commented YAML lines, reading effective values from a default
    ``instance`` (so section-level defaults like cache size=5000G are reflected, not the leaf
    class's own default)."""
    lines: list[str] = []
    pad = "  " * indent
    for name, field in model_cls.model_fields.items():
        ann: Any = field.annotation
        value = getattr(instance, name)
        if isinstance(ann, type) and issubclass(ann, BaseModel):
            lines.append(f"{pad}{name}:")
            lines.extend(_emit_section(value, ann, indent + 1))
        else:
            suffix = f"  # {field.description}" if field.description else ""
            lines.append(f"{pad}{name}: {_yaml_scalar(value)}{suffix}")
    return lines


def render_config_template() -> str:
    """Render a starter config.yaml (nested, with per-field comments) from the model — always in
    sync with LaunchConfig. `chutes-cvm config init` writes this for a miner to edit."""
    header = (
        "# chutes-cvm launch configuration — generated from the schema.\n"
        "# Edit values below, then: chutes-cvm guest launch config.yaml\n"
        "# CLI flags and CHUTES_CVM_* env vars override these at launch.\n"
    )
    # model_construct() gives an instance of pure schema defaults (no env/YAML), so the emitted
    # values reflect the effective defaults (e.g. volumes.cache.size=5000G).
    defaults = LaunchConfig.model_construct()
    return header + "\n".join(_emit_section(defaults, LaunchConfig, 0)) + "\n"


def _cmd_init(args) -> int:
    """`chutes-cvm config init` — write a starter config.yaml (from the schema) to --output."""
    if os.path.exists(args.output) and not args.force:
        print(
            f"chutes-cvm: {args.output} already exists — pass --force to overwrite.",
            file=sys.stderr,
        )
        return 1
    with open(args.output, "w") as f:
        f.write(render_config_template())
    print(
        f"Created {args.output} (generated from the schema). Edit it, then `chutes-cvm guest launch`."
    )
    return 0


def _cmd_verify(args) -> int:
    """`chutes-cvm config verify <file>` — validate a config.yaml against the schema."""
    try:
        LaunchConfig.from_file(args.config_file)
    except ConfigError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    print(f"OK: {args.config_file} is valid")
    return 0


def main(argv=None):
    """`chutes-cvm config <verb>` — manage the launch config.yaml (init / verify)."""
    parser = argparse.ArgumentParser(
        prog="chutes-cvm config", description="Manage the launch config.yaml."
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_init = sub.add_parser(
        "init",
        help="generate a starter config.yaml from the schema (for a miner to edit)",
    )
    p_init.add_argument(
        "-o",
        "--output",
        default="config.yaml",
        help="output path (default: ./config.yaml)",
    )
    p_init.add_argument(
        "--force", action="store_true", help="overwrite an existing file"
    )
    p_init.set_defaults(func=_cmd_init)

    p_verify = sub.add_parser(
        "verify", help="validate a config.yaml against the schema"
    )
    p_verify.add_argument("config_file", help="path to the config.yaml to validate")
    p_verify.set_defaults(func=_cmd_verify)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
