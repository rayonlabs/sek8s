"""Direct-boot artifact resolution for the TDX launcher.

1.4.0+ boots the guest via QEMU ``-kernel``/``-initrd``/``-append`` instead of
GRUB, dropping GRUB/shim from the measured boot chain. OVMF needs the kernel and
initrd as host files.

These are produced **once at build time** (the same bytes
``measurements generate`` measures) and published to R2 alongside the qcow2, so every fleet host downloads
byte-identical boot artifacts — RTMR1/2 match by construction, not by re-running
an extraction on each host at each launch. The launcher just resolves the files
staged next to the image:

    <image-base>.vmlinuz   <image-base>.initrd   <image-base>.cmdline
"""

import os


def direct_boot_artifacts(image_path: str) -> tuple[str, str, str]:
    """Resolve the direct-boot artifacts staged next to ``image_path``.

    Returns ``(kernel_path, initrd_path, cmdline)``. Raises if any are missing —
    they ship with the image (downloaded from R2), so absence means the image
    wasn't fully downloaded or predates direct boot.
    """
    base = os.path.splitext(image_path)[0]
    kernel = base + ".vmlinuz"
    initrd = base + ".initrd"
    cmdline_file = base + ".cmdline"

    missing = [p for p in (kernel, initrd, cmdline_file) if not os.path.exists(p)]
    if missing:
        raise FileNotFoundError(
            "direct-boot artifacts missing next to the image: "
            + ", ".join(missing)
            + " — these are published with the qcow2 (built once, downloaded from "
            "R2). Re-download the image, or stage them via the build's "
            "stage-boot-artifacts step."
        )

    cmdline = open(cmdline_file).read().strip()
    return kernel, initrd, cmdline
