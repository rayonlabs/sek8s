# Cache Volume Setup for TDX VMs

This guide explains how to create and prepare an unencrypted cache volume for use with TDX VMs. The cache volume will be mounted at `/var/snap` in the guest VM to provide additional storage separate from the encrypted root volume.

By default, the guest uses two volumes: **main storage** (mounted at `/cache/storage`) and **HF cache** (mounted at `/var/snap`). Main storage holds the entire **k3s** dir (cluster DB, token, TLS, containerd, init-markers, etc.), the full **kubelet** dir (so node ephemeral-storage capacity reflects the large volume), **admission controller certs**, and **Chutes agent state** under `/cache/storage/k3s`, `/cache/storage/kubelet`, `/cache/storage/admission-controller-certs`, and `/cache/storage/chutes-agent`; these are bind-mounted into `/var/lib/rancher/k3s`, `/var/lib/kubelet`, `/etc/admission-controller/certs`, and `/var/lib/chutes/agent` by `setup-storage-bind-mounts.service`. Keeping k3s and admission controller certs on the storage volume ensures cluster state, node identity, and webhook TLS verification persist across VM upgrades. The HF cache at `/var/snap` is used for application caches (for example `/var/snap/cache` for model weights). The guest runs `setup-cache.service` after the HF cache is mounted; it creates `/var/snap/cache` with ownership `1000:1000` and mode `755` so pods can use it without running as root. Agent state and its bind mount are created and set to `1000:1000`/`755` by `setup-storage-bind-mounts` on main storage.

## Prerequisites

- `qemu-img` (part of QEMU, usually already installed)
- `qemu-nbd` (Network Block Device utility for QEMU)
- `mkfs.xfs` (from xfsprogs package)
- Root/sudo access on the host
- NBD kernel module (standard on most Linux distributions)

## Quick Start

```bash
# Create a 5TB cache volume (raw format, XFS filesystem)
./scripts/volumes/create-cache.sh cache-volume.raw 5000G tdx-cache
```

## Manual Setup (Step-by-Step)

If you prefer to create the volume manually or the script is not available, follow these steps:

### 1. Create the Raw Image

Create an empty raw image file. Adjust the size as needed for your use case:

```bash
qemu-img create -f raw -o preallocation=falloc cache-volume.raw 5000G
```

**Size recommendations:**
- Minimum: 100G
- Recommended: 5T (5000G) or larger for production miners
- Maximum: XFS supports volumes over 16TB (ext4 is limited to ~16TB)

### 2. Load the NBD Kernel Module

The NBD (Network Block Device) module allows us to expose the raw file as a block device:

```bash
sudo modprobe nbd max_part=8
```

**Note:** This only needs to be done once per boot. To make it persistent across reboots, add `nbd` to `/etc/modules`:

```bash
echo "nbd" | sudo tee -a /etc/modules
```

### 3. Connect the Raw Image to an NBD Device

Expose the raw file as a block device:

```bash
sudo qemu-nbd --connect=/dev/nbd0 --format=raw cache-volume.raw
```

**Troubleshooting:**
- If `/dev/nbd0` is busy, try `/dev/nbd1`, `/dev/nbd2`, etc.
- Verify connection: `ls -l /dev/nbd0` should show a block device

### 4. Format with XFS and Label

Format the device with an XFS filesystem and the required label:

```bash
sudo mkfs.xfs -L tdx-cache /dev/nbd0
```

**Important:** The label `tdx-cache` is required. The TDX VM will verify this label at boot time and refuse to start if it's incorrect. XFS labels are limited to 12 characters.

### 5. Disconnect the NBD Device

Clean up by disconnecting the NBD device:

```bash
sudo qemu-nbd --disconnect /dev/nbd0
```

### 6. Verify the Volume (Optional but Recommended)

Reconnect and verify the filesystem and label:

```bash
# Reconnect
sudo qemu-nbd --connect=/dev/nbd0 --format=raw cache-volume.raw

# Verify filesystem and label
sudo blkid /dev/nbd0

# Expected output should include:
# TYPE="xfs" LABEL="tdx-cache"

# Disconnect
sudo qemu-nbd --disconnect /dev/nbd0
```

## Complete Example

Here's a complete example creating a 5TB cache volume:

```bash
# Create the volume
qemu-img create -f raw -o preallocation=falloc /path/to/cache-volume.raw 5000G

# Load NBD module (if not already loaded)
sudo modprobe nbd max_part=8

# Connect to NBD
sudo qemu-nbd --connect=/dev/nbd0 --format=raw /path/to/cache-volume.raw

# Format with label
sudo mkfs.xfs -L tdx-cache /dev/nbd0

# Verify (optional)
sudo blkid /dev/nbd0

# Disconnect
sudo qemu-nbd --disconnect /dev/nbd0

echo "Cache volume ready: /path/to/cache-volume.raw"
```

## Using the Cache Volume

`chutes-cvm guest launch` creates cache and storage volumes automatically based on your `config.yaml`. You normally don't need to create them manually. If you do pre-create a cache volume, reference it in your config:

```yaml
volumes:
  cache:
    size: "5000G"
    path: "/path/to/cache-volume.raw"
```

Then launch as usual:

```bash
chutes-cvm guest launch config.yaml
```

## Verification at Boot

- **Production:** The initramfs script `setup_storage` detects the cache device by label `tdx-cache` (or LUKS label). If the cache device is **not found**, the VM **powers off** (cache is required). If found, it is unlocked (or on first boot encrypted) to `/dev/mapper/tdx-cache`. After boot, `var-snap.mount` mounts it at `/var/snap`, then `verify-cache-volume.service` runs and confirms the mount is from a LUKS dm-crypt device.
- **Debug:** The cache volume is expected unencrypted with label `tdx-cache`. `var-snap.mount` mounts `/dev/disk/by-label/tdx-cache` at `/var/snap`. `verify-cache-volume.service` runs after mount and confirms the device is unencrypted with the correct label.

**If the cache device is missing or verification fails, the VM will shut down.** Check the serial log for error details:

```bash
cat /tmp/tdx-guest-td.log
```

### First-boot seeding of containerd

On the first successful boot with a fresh cache volume, the guest runs the `seed-containerd-cache` systemd unit before k3s starts. This copies the factory containerd state from `/var/lib/rancher/k3s/agent/containerd` (on the encrypted root disk) into `/var/snap/containerd` so that all preloaded images remain available even in offline deployments. A marker file (`/var/snap/containerd/.seeded`) prevents future boots from repeating the expensive copy; you can delete this marker if you intentionally want to reseed the cache after wiping the volume.

## Troubleshooting

### "Device or resource busy" when connecting NBD

Another process is using the NBD device. Try:
```bash
# List NBD devices in use
sudo qemu-nbd --list

# Try a different NBD device
sudo qemu-nbd --connect=/dev/nbd1 --format=raw cache-volume.raw
```

### "No such device" - NBD module not loaded

Load the kernel module:
```bash
sudo modprobe nbd max_part=8
```

### mkfs.xfs command not found

Install XFS utilities:
```bash
# Ubuntu/Debian
sudo apt-get install xfsprogs

# RHEL/CentOS
sudo yum install xfsprogs
```

### Changing the volume size later

You can grow a raw volume (XFS can only be grown, not shrunk):

```bash
# Increase raw image size
qemu-img resize cache-volume.raw +200G

# Connect and grow filesystem
sudo qemu-nbd --connect=/dev/nbd0 --format=raw cache-volume.raw
sudo xfs_growfs /dev/nbd0
sudo qemu-nbd --disconnect /dev/nbd0
```

### Verifying label on an existing volume

If you're unsure if a volume has the correct label:

```bash
sudo qemu-nbd --connect=/dev/nbd0 --format=raw cache-volume.raw
sudo blkid /dev/nbd0 | grep LABEL
sudo qemu-nbd --disconnect /dev/nbd0
```

To change a label on an existing XFS volume:

```bash
sudo qemu-nbd --connect=/dev/nbd0 --format=raw cache-volume.raw
sudo xfs_admin -L tdx-cache /dev/nbd0
sudo qemu-nbd --disconnect /dev/nbd0
```

## Security Considerations

- **Production**: The cache volume is LUKS-encrypted; passphrases are managed by the same API as main storage (separate passphrases per volume). **Debug**: The cache is unencrypted; only store non-sensitive data.
- **Host access**: The host can read unencrypted (debug) cache contents; production cache is encrypted.
- **Integrity**: Consider storing checksums of critical data to detect tampering.
- **Isolation**: Keep cache volumes separate per VM to prevent cross-contamination.

## Storage Best Practices

- **Location**: Store cache volumes on fast storage (SSD/NVMe) for best performance
- **Backups**: The cache is meant for temporary/reproducible data, but back up if needed
- **Raw format**: Raw images allocate full size upfront; use `qemu-img info` to verify
- **Monitoring**: Check actual disk usage with `qemu-img info cache-volume.raw`

## Storage Volume

In addition to the cache volume (HF/model caches at `/var/snap`), the guest requires a **storage volume** for k3s state, containerd data, kubelet pods, admission controller certs, and chutes agent state. This volume is created by `chutes-cvm guest launch` using the same `create-cache.sh` script with the label `storage`. It is configured via `volumes.storage` in `config.yaml`.

See the [host-tools README](../README.md) for the full volume architecture.

## File Locations

- Cache volumes: `host-tools/scripts/cache-<hostname>.raw`
- Storage volumes: `host-tools/scripts/storage-<hostname>.raw`
- Config volumes: `host-tools/scripts/config-<hostname>.qcow2`

## References

- [QEMU NBD documentation](https://qemu.readthedocs.io/en/latest/tools/qemu-nbd.html)
- [qemu-img documentation](https://qemu.readthedocs.io/en/latest/tools/qemu-img.html)
- [XFS filesystem documentation](https://www.kernel.org/doc/html/latest/admin-guide/xfs.html)