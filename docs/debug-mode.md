# Debug Mode Configuration

## Overview

Debug mode allows building VM images without encryption and hardening, making development and debugging easier while still maintaining the same mount structure as production.

## Features

When `debug_build: true` is set:

1. **Skips LUKS encryption** - Root and containerd volumes remain unencrypted
2. **Skips access hardening** - SSH and remote access remain enabled
3. **Unencrypted containerd cache** - No passphrase management needed for debug VMs
4. **Maintains mount structure** - Containerd still mounts to `/var/lib/rancher/k3s/agent/containerd` for consistency

## Configuration

### Enable Debug Mode

Edit `ansible/guest/inventory.yml` to override the default:

```yaml
all:
  vars:
    debug_build: true
```

Or set it per host/group:

```yaml
all:
  hosts:
    vm:
      debug_build: true
```

### Disable Debug Mode (Production)

```yaml
all:
  vars:
    debug_build: false
```

**Default:** `false` (production mode with encryption enabled)

The default is defined in each role's `defaults/main.yml` and can be overridden in inventory.

## Building Images

### Debug Image

```bash
# Set debug_build: true in inventory.yml, then build normally
make build-image

# The playbook will automatically:
# - Run prepare-boot-image in debug mode (debug initramfs, no encryption)
# - Skip the harden-access role (keep SSH access)
# - Configure containerd cache for unencrypted device
```

### Production Image

```bash
# Set debug_build: false in inventory.yml (or use default), then build
make build-image

# The playbook will:
# - Run prepare-boot-image (encrypt root + boot scripts + measured initramfs)
# - Run the harden-access role (remove SSH access)
# - Configure containerd cache for encrypted device with attestation
```

## Implementation Details

### Roles Affected

1. **prepare-boot-image role** - Runs `debug.yml` (no encryption) when `debug_build: true`
   - No root encryption
   - Fail-open debug initramfs installed instead of the LUKS-unlock prod one
   - No attestation setup

2. **harden-access role** - Skipped when `debug_build: true`
   - SSH access remains enabled
   - Remote access not removed

3. **cache-volume role** - Adapted for both modes
   - Production: Uses `/dev/mapper/containerd_cache` (encrypted)
   - Debug: Uses device by label `containerd-cache` (unencrypted)
   - Script detects mode via `DEBUG_MODE` environment variable

### Containerd Cache Behavior

#### Production Mode (`debug_build: false`)
- Boot script (`setup_containerd_cache`) unlocks containerd device using validator API
- Init service copies data from encrypted mapper device
- Mount unit mounts `/dev/mapper/containerd_cache`

#### Debug Mode (`debug_build: true`)
- No boot script needed (device not encrypted)
- Init service detects unencrypted device by label and copies data directly
- Mount unit still mounts to same path for consistency

### Service Configuration

The `containerd-cache-init.service` automatically receives the `DEBUG_MODE` environment variable from Ansible templating:

```ini
[Service]
Environment="DEBUG_MODE=true"   # or "false" based on ansible variable
ExecStart=/usr/local/bin/init-containerd-cache.sh
```

The init script (`init-containerd-cache.sh`) detects the mode:

```bash
if [ "$DEBUG_MODE" = "true" ]; then
    # Use unencrypted device directly
    DEVICE=$(blkid -l -o device -t LABEL="containerd-cache")
else
    # Use encrypted mapper device
    DEVICE="/dev/mapper/containerd_cache"
fi
```

## Creating Debug VMs

### Download the Debug Image

```bash
cd host-tools/scripts
chutes-cvm image download --debug
```

This downloads the debug image set (qcow2 + boot artifacts + `manifest.json`) into
`/var/lib/chutes/base-images/tdx-guest-debug/` and verifies it against the manifest.

### Launch with chutes-cvm

Use the debug example config as a starting point:

```bash
cd host-tools/scripts
cp config/config.debug.example.yaml config.yaml
# Edit config.yaml with your credentials and network settings
chutes-cvm guest launch config.yaml --foreground
```

The debug config sets `vm.base_image` to the debug image path and uses smaller volume sizes. See [`config/config.debug.example.yaml`](../host-tools/scripts/config/config.debug.example.yaml) for the full template.

## Benefits

### Development
- No passphrase management for debug VMs
- SSH access for debugging
- Faster boot (no attestation/encryption)
- Same containerd mount structure as production

### Testing
- Can test containerd cache behavior without encryption complexity
- Validate mount timing and data migration
- Debug systemd service dependencies

### Production Parity
- Containerd mounts to same location
- Same systemd service chain
- Same directory structure
- Only encryption differs

## Security Notes

⚠️ **Debug images should NEVER be used in production**

Debug images:
- Have no encryption (data at rest is readable)
- Have SSH access enabled (remote access possible)
- Skip security hardening
- Are intended for development/testing only

Always verify `debug_build: false` before building production images.

## Troubleshooting

### Debug image not skipping encryption
- Verify `debug_build: true` in `inventory.yml`
- Check Ansible output for "SKIPPED" on luks/harden-access tasks
- Ensure no manual tag overrides (`--tags luks` would force it)

### Containerd cache not mounting
- Check `journalctl -u containerd-cache-init.service` for errors
- Verify device label: `blkid | grep containerd-cache`
- Confirm DEBUG_MODE in service: `systemctl show containerd-cache-init.service | grep Environment`

### SSH access not available in debug image
- Verify harden-access role was skipped in build logs
- Check if `debug_build: true` was set before build
- Rebuild image with correct debug_build setting
