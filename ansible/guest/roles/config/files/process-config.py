#!/usr/bin/env python3
"""
validate-config.py - Secure config volume validator for TEE TDX VMs
Validates config files from mounted config volume and sets up system configuration
"""

import base64
import grp
import json
import os
import re
import shutil
import sys
import yaml
from datetime import datetime
from pathlib import Path

# Configuration Constants
CONFIG_MOUNT_DIR = "/var/config"
BACKUP_DIR = "/var/lib/config-backups"
LOG_FILE = "/var/log/config-validator.log"

# Expected config files in the volume
EXPECTED_FILES = {
    "hostname": "/var/config/hostname",
    "miner-ss58": "/var/config/miner-ss58", 
    "miner-seed": "/var/config/miner-seed",
    "network-config.yaml": "/var/config/network-config.yaml"
}

# Target paths for configuration
HOSTNAME_TARGET = "/etc/hostname"
MINER_CREDS_DIR = "/var/lib/rancher/k3s/credentials"
MINER_SS58_TARGET = os.path.join(MINER_CREDS_DIR, "miner-ss58")
MINER_SEED_TARGET = os.path.join(MINER_CREDS_DIR, "miner-seed")
# Separate env file for system-manager miner vars only (cache pre-download signing).
# systemd loads this in addition to /etc/system-manager/system-manager.env (build-time);
# we never modify system-manager.env.
SYSTEM_MANAGER_MINER_ENV = "/etc/system-manager/miner.env"
NETWORK_CONFIG_TARGET = "/etc/netplan/50-config-volume.yaml"

# Optional Docker Hub credentials (config volume); guest applies to cosign + k3s registries
DOCKER_HUB_USER_FILE = os.path.join(CONFIG_MOUNT_DIR, "docker-hub-username")
DOCKER_HUB_TOKEN_FILE = os.path.join(CONFIG_MOUNT_DIR, "docker-hub-token")
DOCKER_CONFIG_DIR = "/etc/admission-controller/docker-config"
DOCKER_CONFIG_JSON = os.path.join(DOCKER_CONFIG_DIR, "config.json")
REGISTRIES_YAML = "/etc/rancher/k3s/registries.yaml"
DOCKER_HUB_AUTH_KEY = "https://index.docker.io/v1/"
# k3s/containerd may match any of these hosts for Hub API traffic.
HUB_REGISTRY_CONFIG_KEYS = (
    "docker.io",
    "registry-1.docker.io",
    "index.docker.io",
)
# Docker Hub IDs are short in practice (~30); cap avoids huge untrusted blobs.
MAX_DOCKER_HUB_USERNAME_LEN = 64
# PATs are short (often ~36 chars); 128 leaves slack for format changes and password login.
MAX_DOCKER_HUB_TOKEN_LEN = 128

def log(message, level="INFO"):
    """Log a message with timestamp"""
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    log_entry = f"[{timestamp}] {level}: {message}"
    print(log_entry)
    
    # Ensure log directory exists
    os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
    
    # Write to log file
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(log_entry + "\n")

def validate_ss58_address(address):
    """Validate SS58 address format for Bittensor network.

    This validation is intentionally duplicated in the initramfs shell script
    ansible/guest/roles/prepare-boot-image/files/initramfs/write-validator-auth which runs
    before Python is available.  If you change any of the three criteria below,
    update the shell script to match.

    Criteria (all three must hold):
      1. Length: 40–50 characters
      2. Charset: every character in the base58 alphabet
         (123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz)
      3. Prefix: starts with '5'  (Bittensor mainnet, network prefix 42)
    """
    if not isinstance(address, str):
        return False, "SS58 address must be a string"

    address = address.strip()

    # Criterion 1: length
    if len(address) < 40 or len(address) > 50:
        return False, f"SS58 address length invalid: {len(address)} (expected 40-50 chars)"

    # Criterion 2: base58 charset
    _SS58_CHARS = frozenset("123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz")
    invalid = [c for c in address if c not in _SS58_CHARS]
    if invalid:
        return False, f"SS58 address contains invalid characters: {invalid}"

    # Criterion 3: Bittensor mainnet prefix
    if not address.startswith('5'):
        return False, "SS58 address should start with '5' for Bittensor mainnet"

    return True, "SS58 address is valid"

def validate_seed_content(seed):
    """Validate seed content (hex string without 0x prefix)"""
    if not isinstance(seed, str):
        return False, "Seed must be a string"
    
    # Remove whitespace
    seed = seed.strip()
    
    # Check if it accidentally has 0x prefix (should be removed)
    if seed.startswith('0x') or seed.startswith('0X'):
        return False, "Seed should not have '0x' prefix"
    
    # Seed should be hex string, typically 64 characters (32 bytes)
    if len(seed) != 64:
        return False, f"Seed length invalid: {len(seed)} (expected 64 hex characters)"
    
    # Validate hex characters
    if not re.match(r'^[a-fA-F0-9]+$', seed):
        return False, "Seed contains invalid hex characters"
    
    return True, "Seed is valid"

def validate_hostname(hostname):
    """Validate hostname follows RFC standards and security requirements"""
    if not isinstance(hostname, str):
        return False, "Hostname must be a string"
    
    # Remove whitespace
    hostname = hostname.strip()
    
    if len(hostname) > 63:
        return False, "Hostname too long (max 63 characters)"
    
    if not re.match(r'^[a-zA-Z0-9-]+$', hostname):
        return False, "Hostname contains invalid characters"
    
    if hostname.startswith('-') or hostname.endswith('-'):
        return False, "Hostname cannot start or end with hyphen"
    
    return True, "Hostname is valid"

def validate_network_config(network_config):
    """Validate network configuration YAML"""
    try:
        content = yaml.safe_load(network_config)
    except yaml.YAMLError as e:
        return False, f"Invalid YAML: {e}"
    
    if not isinstance(content, dict):
        return False, "Network config must be a dictionary"
    
    config: dict = content['network']
    # Check for required version
    if config.get('version') != 2:
        return False, "Network config must have version: 2"
    
    # Validate ethernets section
    if 'ethernets' not in config:
        return False, "Network config must have 'ethernets' section"
    
    ethernets = config['ethernets']
    if not isinstance(ethernets, dict):
        return False, "ethernets must be a dictionary"
    
    # For now, just validate structure - don't enforce specific interface names
    for interface, settings in ethernets.items():
        if not isinstance(settings, dict):
            return False, f"Settings for interface {interface} must be a dictionary"
        
        # Allow either static config (addresses) or DHCP
        has_addresses = 'addresses' in settings
        has_dhcp = settings.get('dhcp4') is True or settings.get('dhcp6') is True
        
        if not has_addresses and not has_dhcp:
            return False, f"Interface {interface} must have either addresses or DHCP enabled"
    
    return True, "Network config is valid"


def validate_docker_hub_string(value, max_len, field_label):
    """Parser-safe string check: non-empty after strip, bounded length, no C0 controls (incl. NUL)."""
    if not isinstance(value, str):
        return None, f"{field_label} must be a string"
    stripped = value.strip()
    if not stripped:
        return None, f"{field_label} is empty"
    if len(stripped) > max_len:
        return None, f"{field_label} exceeds max length ({max_len})"
    if any(ord(c) < 32 for c in stripped):
        return None, f"{field_label} contains control characters"
    return stripped, None


def read_docker_hub_credentials():
    """
    Read optional Docker Hub username/token from config volume.
    Returns (username, token) both str if valid, or (None, None) to use anonymous Hub.
    """
    have_user = os.path.isfile(DOCKER_HUB_USER_FILE)
    have_token = os.path.isfile(DOCKER_HUB_TOKEN_FILE)
    if not have_user and not have_token:
        return None, None
    if not have_user or not have_token:
        log("Docker Hub: need both docker-hub-username and docker-hub-token; skipping Hub auth", "WARNING")
        return None, None

    user_raw = read_config_file(DOCKER_HUB_USER_FILE)
    token_raw = read_config_file(DOCKER_HUB_TOKEN_FILE)
    if user_raw is None or token_raw is None:
        log("Docker Hub: failed to read credential files; skipping Hub auth", "WARNING")
        return None, None

    user, u_err = validate_docker_hub_string(user_raw, MAX_DOCKER_HUB_USERNAME_LEN, "docker_hub.username")
    if u_err:
        log(f"Docker Hub username invalid ({u_err}); skipping Hub auth", "WARNING")
        return None, None
    token, t_err = validate_docker_hub_string(token_raw, MAX_DOCKER_HUB_TOKEN_LEN, "docker_hub.token")
    if t_err:
        log(f"Docker Hub token invalid ({t_err}); skipping Hub auth", "WARNING")
        return None, None

    return user, token


def apply_docker_hub_and_registries(username, token):
    """
    Write Docker config.json (always) and merge Docker Hub auth into registries.yaml.
    username and token are both set (str) for authenticated Hub, or both None for anonymous.
    """
    try:
        admission_gid = grp.getgrnam("admission").gr_gid
    except KeyError:
        if username is None and token is None:
            # No admission controller and no Docker Hub creds — nothing to do.
            log("Group 'admission' not found and no Docker Hub credentials present — skipping docker config")
            return True
        log("Group 'admission' not found; cannot configure docker-config", "ERROR")
        return False

    try:
        os.makedirs(DOCKER_CONFIG_DIR, mode=0o750, exist_ok=True)
        os.chmod(DOCKER_CONFIG_DIR, 0o750)
        os.chown(DOCKER_CONFIG_DIR, 0, admission_gid)
    except OSError as e:
        log(f"Failed to prepare {DOCKER_CONFIG_DIR}: {e}", "ERROR")
        return False

    if username is not None and token is not None:
        auth_b64 = base64.b64encode(f"{username}:{token}".encode("utf-8")).decode("ascii")
        docker_cfg = {"auths": {DOCKER_HUB_AUTH_KEY: {"auth": auth_b64}}}
    else:
        docker_cfg = {"auths": {}}

    try:
        with open(DOCKER_CONFIG_JSON, "w", encoding="utf-8") as f:
            f.write(json.dumps(docker_cfg))
        os.chmod(DOCKER_CONFIG_JSON, 0o640)
        os.chown(DOCKER_CONFIG_JSON, 0, admission_gid)
        log(
            f"Wrote {DOCKER_CONFIG_JSON} (hub_auth={'yes' if username else 'no'})",
            "INFO",
        )
    except OSError as e:
        log(f"Failed to write {DOCKER_CONFIG_JSON}: {e}", "ERROR")
        return False

    if not os.path.isfile(REGISTRIES_YAML):
        log(f"{REGISTRIES_YAML} not found; skipping registries merge (docker config applied)", "WARNING")
        return True

    try:
        with open(REGISTRIES_YAML, "r", encoding="utf-8") as f:
            raw = f.read()
        data = yaml.safe_load(raw) if raw.strip() else {}
        if data is None:
            data = {}
        if not isinstance(data, dict):
            log("registries.yaml root is not a mapping; skipping Hub merge", "WARNING")
            return True

        configs = data.get("configs")
        if configs is None:
            configs = {}
            data["configs"] = configs
        elif not isinstance(configs, dict):
            log("registries.yaml 'configs' is not a mapping; skipping Hub merge", "WARNING")
            return True

        for key in HUB_REGISTRY_CONFIG_KEYS:
            configs.pop(key, None)

        if username is not None and token is not None:
            for key in HUB_REGISTRY_CONFIG_KEYS:
                configs[key] = {
                    "auth": {"username": username, "password": token},
                }

        out = yaml.safe_dump(data, default_flow_style=False, sort_keys=False, allow_unicode=True)
        with open(REGISTRIES_YAML, "w", encoding="utf-8") as f:
            f.write(out)
        log("Merged Docker Hub auth into registries.yaml", "INFO")
    except (yaml.YAMLError, OSError) as e:
        log(f"Failed to merge {REGISTRIES_YAML}: {e}", "ERROR")
        return False

    return True


def create_backup_dir():
    """Create backup directory"""
    try:
        os.makedirs(BACKUP_DIR, exist_ok=True)
        return True
    except Exception as e:
        log(f"Failed to create backup directory: {e}", "ERROR")
        return False

def read_config_file(filepath):
    """Read and return contents of config file"""
    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            return f.read().strip()
    except Exception as e:
        log(f"Failed to read {filepath}: {e}", "ERROR")
        return None

def write_target_file(content, target_path, mode=0o644, owner_uid=0, owner_gid=0):
    """Write content to target file with specified permissions"""
    try:
        # Ensure target directory exists
        target_dir = os.path.dirname(target_path)
        os.makedirs(target_dir, exist_ok=True)
        
        # Write new content
        with open(target_path, 'w', encoding='utf-8') as f:
            f.write(content)
        
        # Set permissions and ownership
        os.chmod(target_path, mode)
        os.chown(target_path, owner_uid, owner_gid)
        
        log(f"Successfully wrote {target_path}")
        return True
    except Exception as e:
        log(f"Failed to write {target_path}: {e}", "ERROR")
        return False

def clear_netplan_directory():
    """Clear netplan directory and ensure clean state"""
    try:
        netplan_dir = "/etc/netplan"
        
        # Remove all files in netplan directory
        if os.path.exists(netplan_dir):
            for filename in os.listdir(netplan_dir):
                file_path = os.path.join(netplan_dir, filename)
                if os.path.isfile(file_path):
                    os.remove(file_path)
                    log(f"Removed old netplan file: {filename}")
        else:
            # Create directory if it doesn't exist
            os.makedirs(netplan_dir, exist_ok=True)
        
        log("Netplan directory cleared")
        return True
    except Exception as e:
        log(f"Failed to clear netplan directory: {e}", "ERROR")
        return False

def validate_and_apply_config():
    """Main validation and configuration function"""
    log("Starting config volume validation")
    
    # Check if config mount directory exists and is mounted
    if not os.path.ismount(CONFIG_MOUNT_DIR):
        log(f"Config volume not mounted at {CONFIG_MOUNT_DIR}", "ERROR")
        return False
    
    # Clear netplan directory first
    if not clear_netplan_directory():
        return False
    
    # Check required files exist (hostname and network config are always required;
    # miner credentials are optional — absent in benchmark mode).
    required_files = ["hostname", "network-config.yaml"]
    missing_files = [EXPECTED_FILES[k] for k in required_files if not os.path.isfile(EXPECTED_FILES[k])]
    if missing_files:
        log(f"Missing required config files: {missing_files}", "ERROR")
        return False

    has_miner_creds = (
        os.path.isfile(EXPECTED_FILES["miner-ss58"]) and
        os.path.isfile(EXPECTED_FILES["miner-seed"])
    )
    if not has_miner_creds:
        log("Miner credential files absent — running in benchmark mode (no k3s credential setup)")

    # Validate hostname
    hostname_content = read_config_file(EXPECTED_FILES["hostname"])
    if hostname_content is None:
        return False
    
    is_valid, msg = validate_hostname(hostname_content)
    if not is_valid:
        log(f"Invalid hostname: {msg}", "ERROR")
        return False
    log(f"Hostname validation passed: {hostname_content}")

    # Validate miner credentials (only when present)
    ss58_content = None
    seed_content = None
    if has_miner_creds:
        ss58_content = read_config_file(EXPECTED_FILES["miner-ss58"])
        if ss58_content is None:
            return False
        is_valid, msg = validate_ss58_address(ss58_content)
        if not is_valid:
            log(f"Invalid miner SS58: {msg}", "ERROR")
            return False
        log("Miner SS58 validation passed")

        seed_content = read_config_file(EXPECTED_FILES["miner-seed"])
        if seed_content is None:
            return False
        is_valid, msg = validate_seed_content(seed_content)
        if not is_valid:
            log(f"Invalid miner seed: {msg}", "ERROR")
            return False
        log("Miner seed validation passed")
    
    # Validate network config
    network_content = read_config_file(EXPECTED_FILES["network-config.yaml"])
    if network_content is None:
        return False
    
    is_valid, msg = validate_network_config(network_content)
    if not is_valid:
        log(f"Invalid network config: {msg}", "ERROR")
        return False
    log("Network config validation passed")
    
    # All validations passed - apply configuration
    log("All validations passed, applying configuration...")

    # Apply hostname
    if not write_target_file(hostname_content + "\n", HOSTNAME_TARGET, 0o644):
        return False

    # Apply miner credentials and system-manager env (production only)
    if has_miner_creds:
        if not write_target_file(ss58_content + "\n", MINER_SS58_TARGET, 0o600):
            return False
        if not write_target_file(seed_content + "\n", MINER_SEED_TARGET, 0o600):
            return False
        SYSTEM_MANAGER_GID = 10150
        miner_env_content = f"MINER_SS58={ss58_content}\nMINER_SEED={seed_content}\n"
        if not write_target_file(miner_env_content, SYSTEM_MANAGER_MINER_ENV, 0o640, owner_uid=0, owner_gid=SYSTEM_MANAGER_GID):
            return False

    # Apply network config
    if not write_target_file(network_content, NETWORK_CONFIG_TARGET, 0o600):
        return False

    # Docker Hub (optional): always refresh config.json + Hub keys in registries.yaml
    hub_user, hub_token = read_docker_hub_credentials()
    if not apply_docker_hub_and_registries(hub_user, hub_token):
        return False
    
    # Set hostname immediately
    try:
        with open('/proc/sys/kernel/hostname', 'w') as f:
            f.write(hostname_content)
        log("Hostname applied immediately")
    except Exception as e:
        log(f"Warning: Could not set hostname immediately: {e}", "WARNING")
    
    log("Configuration applied successfully")
    return True

def main():
    """Main entry point"""
    try:
        # Ensure we're running as root for security operations
        if os.geteuid() != 0:
            log("This script must be run as root", "ERROR")
            sys.exit(1)
        
        # Validate and apply configuration
        if validate_and_apply_config():
            log("Config validation and application completed successfully")
            sys.exit(0)
        else:
            log("Config validation failed", "ERROR")
            sys.exit(1)
            
    except KeyboardInterrupt:
        log("Validation interrupted by user", "ERROR")
        sys.exit(1)
    except Exception as e:
        log(f"Unexpected error: {e}", "ERROR")
        sys.exit(1)

if __name__ == "__main__":
    main()