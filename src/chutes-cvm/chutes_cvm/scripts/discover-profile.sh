#!/usr/bin/env bash
# discover-profile.sh — Probe host hardware and report GPU metrics.
#
# Collects raw hardware facts (GPU device IDs, CPU topology, NUMA layout,
# InfiniBand, NVSwitches) and writes them to a JSON file. No profile matching
# or verification is performed — use the Python module for that.
#
# Usage:
#   ./discover-profile.sh              # Report to terminal + JSON in CWD
#   ./discover-profile.sh --json-only  # Skip terminal report
#   ./discover-profile.sh --no-json    # Skip JSON output
#
# No root required. nvidia-smi must be accessible (driver loaded).
set -euo pipefail

JSON_OUTPUT=1
REPORT_OUTPUT=1

for arg in "$@"; do
    case "$arg" in
        --json-only) REPORT_OUTPUT=0 ;;
        --no-json)   JSON_OUTPUT=0 ;;
        --help|-h)
            echo "Usage: $0 [--json-only | --no-json]"
            exit 0
            ;;
        *) echo "Unknown argument: $arg"; exit 1 ;;
    esac
done

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

have() { command -v "$1" &>/dev/null; }

bar() {
    if [[ $REPORT_OUTPUT -eq 1 ]]; then
        printf '%s\n' "$(printf '─%.0s' {1..72})"
    fi
}

section() {
    if [[ $REPORT_OUTPUT -eq 1 ]]; then
        echo ""
        printf '\033[1;36m%s\033[0m\n' "$1"
        bar
    fi
}

row() {
    if [[ $REPORT_OUTPUT -eq 1 ]]; then
        printf '  %-32s %s\n' "$1" "$2"
    fi
}

warn() {
    if [[ $REPORT_OUTPUT -eq 1 ]]; then
        printf '  \033[33m⚠  %s\033[0m\n' "$1"
    fi
}

# Convert "Xg" / "XG" / "XM" lspci size string to MiB integer
size_to_mib() {
    local s="${1^^}"   # uppercase
    if [[ "$s" =~ ^([0-9]+)G$ ]]; then
        echo $(( ${BASH_REMATCH[1]} * 1024 ))
    elif [[ "$s" =~ ^([0-9]+)M$ ]]; then
        echo "${BASH_REMATCH[1]}"
    elif [[ "$s" =~ ^([0-9]+)K$ ]]; then
        echo $(( ${BASH_REMATCH[1]} / 1024 ))
    else
        echo "-1"
    fi
}

# Read sysfs NUMA node for a PCI BDF (returns -1 if unknown)
pci_numa_node() {
    local bdf="$1"
    local p="/sys/bus/pci/devices/${bdf}/numa_node"
    if [[ -r "$p" ]]; then
        cat "$p"
    else
        echo "-1"
    fi
}

# Read sysfs cpulist for a NUMA node
numa_cpulist() {
    local node="$1"
    local p="/sys/devices/system/node/node${node}/cpulist"
    [[ -r "$p" ]] && cat "$p" || echo "?"
}

# ---------------------------------------------------------------------------
# CPU topology
# ---------------------------------------------------------------------------
CPU_TOTAL=$(lscpu | awk '/^CPU\(s\):/ {print $2}')
CPU_SOCKETS=$(lscpu | awk '/^Socket\(s\):/ {print $2}')
CPU_CORES_PER_SOCKET=$(lscpu | awk '/^Core\(s\) per socket:/ {print $NF}')
CPU_THREADS_PER_CORE=$(lscpu | awk '/^Thread\(s\) per core:/ {print $NF}')

# CPU identity for offline RTMR0 measurement reconstruction (the profile's
# cpu_vendor / cpu_processor_id). RTMR0 is the only CPU-dependent measurement, and
# only two things move it: the vendor drives #13 (QEMU shoves high memory past 1 TiB
# for AMD guests → different SRAT), and the SMBIOS Type-4 Processor ID (#14) is the
# guest's CPUID leaf-1. (phys-bits was measured to NOT affect RTMR0, so it is not
# captured.) Both are CPU-model properties — computed here host-side, no TDX and no
# guest capture. Set them once on the profile (they never vary by build or topology,
# which is why the original single-CCEL method reused one #14 everywhere).
CPU_VENDOR=$(lscpu | awk -F: '/^Vendor ID:/ {gsub(/^[ \t]+/,"",$2); print $2}')
# SMBIOS Type-4 Processor ID = CPUID leaf-1 EAX|EDX as the TDX guest sees it:
#   EAX = family/model/stepping (native — TDX passes it through), from lscpu.
#   EDX = the TDX Module's fixed leaf-1 CPUID-virtualization baseline (NOT the host's
#         raw EDX — TDX masks host-specific bits like PSE36). Constant for Intel TDX;
#         re-verify against the TDX Module spec (or one real boot) only if the TDX
#         module version changes — never per host/build.
_TDX_LEAF1_EDX="0x1fa9fbff"
_CPU_FAMILY=$(lscpu | awk -F: '/^CPU family:/ {gsub(/ /,"",$2);print $2}')
_CPU_MODEL=$(lscpu | awk -F: '/^Model:/ {gsub(/ /,"",$2);print $2}')
_CPU_STEPPING=$(lscpu | awk -F: '/^Stepping:/ {gsub(/ /,"",$2);print $2}')
CPU_PROCESSOR_ID=$(python3 - "$_CPU_FAMILY" "$_CPU_MODEL" "$_CPU_STEPPING" "$_TDX_LEAF1_EDX" <<'PY' 2>/dev/null || true
import sys
fam, mod, step = int(sys.argv[1]), int(sys.argv[2]), int(sys.argv[3])
edx = int(sys.argv[4], 16)
base_fam = fam if fam < 0xF else 0xF
ext_fam = (fam - 0xF) if fam >= 0xF else 0
ext_model, mod_lo = mod >> 4, mod & 0xF
eax = (step & 0xF) | (mod_lo << 4) | ((base_fam & 0xF) << 8) | ((ext_model & 0xF) << 16) | ((ext_fam & 0xFF) << 20)
print((eax.to_bytes(4, "little") + edx.to_bytes(4, "little")).hex())
PY
)

# ---------------------------------------------------------------------------
# Memory
# ---------------------------------------------------------------------------
MEM_TOTAL_KB=$(awk '/^MemTotal:/ {print $2}' /proc/meminfo)
MEM_TOTAL_GB=$(( MEM_TOTAL_KB / 1024 / 1024 ))

# ---------------------------------------------------------------------------
# OS release + the QEMU -cpu args it launches with
# Mirrors chutes-cvm guest launch (GUEST_CPU_ARGS in chutes_cvm/guest/detection.py): every
# supported release takes the avx10 mask, so this is a constant — keep the two in step, since
# -cpu shapes the CPUID leaves the guest sees. The release itself is still captured: it is an
# RTMR0-relevant input (it picks the host QEMU), so a divergence traces back to host OS drift.
# ---------------------------------------------------------------------------
OS_VERSION_ID=""
if [[ -r /etc/os-release ]]; then
    OS_VERSION_ID=$(. /etc/os-release 2>/dev/null; printf '%s' "${VERSION_ID:-}")
fi
CPU_ARGS="host,-avx10"

# ---------------------------------------------------------------------------
# Host QEMU version — a primary RTMR0 determinant.
# QEMU generates the guest ACPI tables (SRAT/SLIT/DSDT) and TD HOB that TDVF
# measures into RTMR0. Two hosts with a byte-identical launch command but
# different QEMU builds (e.g. Ubuntu 25.10 → 10.1.0 vs 26.04 → 10.2.1) extend
# DIFFERENT RTMR0s. Capture both the bare version and the full distro string.
# ---------------------------------------------------------------------------
QEMU_VERSION=""
QEMU_VERSION_FULL=""
if have qemu-system-x86_64; then
    QEMU_VERSION_FULL=$(qemu-system-x86_64 --version 2>/dev/null | head -1 || true)
    QEMU_VERSION=$(printf '%s' "$QEMU_VERSION_FULL" | grep -oP 'version \K[0-9]+(\.[0-9]+)*' || true)
fi

# ---------------------------------------------------------------------------
# DMI baseboard + BIOS identity (world-readable sysfs — no root required)
# Baseboard model and BIOS version pin down "identical" servers that may differ
# in firmware/BIOS settings (e.g. Sub-NUMA Clustering) which reshape the guest
# memory map and therefore RTMR0.
# ---------------------------------------------------------------------------
dmi() {
    local f="/sys/class/dmi/id/$1"
    [[ -r "$f" ]] && tr -d '\n' < "$f" || echo ""
}
BOARD_VENDOR=$(dmi board_vendor)
BOARD_NAME=$(dmi board_name)
BIOS_VENDOR=$(dmi bios_vendor)
BIOS_VERSION=$(dmi bios_version)
BIOS_DATE=$(dmi bios_date)
PRODUCT_NAME=$(dmi product_name)

# ---------------------------------------------------------------------------
# NUMA nodes
# ---------------------------------------------------------------------------
NUMA_NODES=()
if [[ -d /sys/devices/system/node ]]; then
    for n in /sys/devices/system/node/node[0-9]*; do
        [[ -d "$n" ]] && NUMA_NODES+=("${n##*/node}")
    done
fi
NUMA_NODE_COUNT=${#NUMA_NODES[@]}

# numa_active gate — mirrors qemu.use_numa_topology(): profiles that request
# guest NUMA topology (H200/B200/RTX) only get per-node memory backends + guest
# SRAT/SLIT + PXB-PCIe grouping when the host has EXACTLY 2 NUMA nodes. Any other
# count (e.g. Sub-NUMA Clustering → 4) falls back to a flat memory map + numactl
# interleave. The two layouts emit DIFFERENT guest ACPI/memory tables, so they
# extend DIFFERENT RTMR0 values. This is the most common cause of an otherwise
# "identical" host pair attesting two different RTMR0s.
if [[ $NUMA_NODE_COUNT -eq 2 ]]; then
    NUMA_TOPOLOGY_ELIGIBLE="yes"
else
    NUMA_TOPOLOGY_ELIGIBLE="no"
fi

declare -A NUMA_CPULIST
for n in "${NUMA_NODES[@]}"; do
    NUMA_CPULIST[$n]=$(numa_cpulist "$n")
done

# ---------------------------------------------------------------------------
# NVIDIA GPUs
# ---------------------------------------------------------------------------
GPU_BDFS=()
GPU_DEVICE_IDS=()
GPU_NUMA_NODES=()

while IFS= read -r line; do
    # e.g. "0000:a1:00.0 3D controller [0302]: NVIDIA ... [10de:2901]"
    bdf=$(echo "$line" | awk '{print $1}')
    dev_id=$(echo "$line" | grep -oP '10de:\K[0-9a-fA-F]{4}')
    numa_n=$(pci_numa_node "$bdf")
    GPU_BDFS+=("$bdf")
    GPU_DEVICE_IDS+=("$dev_id")
    GPU_NUMA_NODES+=("$numa_n")
done < <(lspci -Dnn 2>/dev/null | grep '10de' | grep -E '\[030[02]\]' || true)

GPU_COUNT=${#GPU_BDFS[@]}

# Unique device IDs
declare -A UNIQ_IDS
if [[ ${#GPU_DEVICE_IDS[@]} -gt 0 ]]; then
    for id in "${GPU_DEVICE_IDS[@]}"; do
        UNIQ_IDS[$id]=1
    done
fi
UNIQUE_DEVICE_IDS=("${!UNIQ_IDS[@]}")

# BAR size from first GPU
BAR_SIZE_MB=""
if [[ $GPU_COUNT -gt 0 ]]; then
    bar_line=$(lspci -vvv -s "${GPU_BDFS[0]}" 2>/dev/null | grep -m1 'Region 2' || true)
    if [[ -n "$bar_line" ]]; then
        bar_size_str=$(echo "$bar_line" | grep -oP 'size=\K\S+' | head -1 || true)
        [[ -n "$bar_size_str" ]] && BAR_SIZE_MB=$(size_to_mib "$bar_size_str")
    fi
fi

# Full PCI BAR layout of the first GPU, for the profile's passthrough["gpu"] spec
# (offline measurement generation reproduces these windows with a pci-bar-stub). The
# Region lines already report a resizable BAR's *current* size, so no separate
# Resizable-BAR parse is needed. Emitted as a copy-pasteable PciBar(...) list.
GPU_PCI_BARS_SNIPPET=""
if [[ $GPU_COUNT -gt 0 ]]; then
    while IFS= read -r bar_line; do
        [[ "$bar_line" =~ Region\ ([0-9]+):\ Memory.*\((32|64)-bit,\ (non-prefetchable|prefetchable)\).*size=([0-9A-Za-z]+) ]] || continue
        bar_idx="${BASH_REMATCH[1]}"
        bar_width="${BASH_REMATCH[2]}"
        bar_pref="${BASH_REMATCH[3]}"
        bar_mib=$(size_to_mib "${BASH_REMATCH[4]}")
        bar_kind="m${bar_width}"
        [[ "$bar_pref" == "prefetchable" ]] && bar_kind="p${bar_width}"
        GPU_PCI_BARS_SNIPPET+="        PciBar(${bar_idx}, ${bar_mib}, \"${bar_kind}\"),"$'\n'
    done < <(lspci -vvv -s "${GPU_BDFS[0]}" 2>/dev/null | grep -E 'Region [0-9]+: Memory')
fi

# VRAM per GPU via nvidia-smi
VRAM_MIB=""
VRAM_GB=""
if have nvidia-smi; then
    vram_line=$(nvidia-smi --query-gpu=memory.total --format=csv,noheader 2>/dev/null | head -1 | tr -d ' ' || true)
    if [[ "$vram_line" =~ ^([0-9]+)MiB$ ]]; then
        VRAM_MIB="${BASH_REMATCH[1]}"
        VRAM_GB=$(( (VRAM_MIB + 512) / 1024 ))
    fi
else
    warn "nvidia-smi not found — VRAM not probed (driver may not be loaded)"
fi

# Suggested ram_per_gpu_gb: VRAM or memory-based fallback
if [[ -n "$VRAM_GB" && $GPU_COUNT -gt 0 ]]; then
    SUGGESTED_RAM_PER_GPU=$VRAM_GB
elif [[ $GPU_COUNT -gt 0 ]]; then
    fallback=$(( (MEM_TOTAL_GB - 64) / GPU_COUNT ))
    SUGGESTED_RAM_PER_GPU=$(( fallback > 0 ? fallback : MEM_TOTAL_GB / GPU_COUNT ))
else
    SUGGESTED_RAM_PER_GPU=0
fi

# ---------------------------------------------------------------------------
# Per-GPU VBIOS version, keyed by PCI BDF.
# nvidia-smi enumerates GPUs in its own index order, which is not guaranteed to
# match lspci's, so map by bus id rather than positional index. VBIOS does NOT
# feed RTMR0 (it lives in the GPU/nvtrust attestation path) — captured here as
# part of the GPU evidence picture, not as an RTMR0 differentiator.
# ---------------------------------------------------------------------------
declare -A VBIOS_BY_BDF
if have nvidia-smi; then
    while IFS=, read -r busid vbios; do
        busid=$(echo "$busid" | tr -d ' ' | tr 'A-F' 'a-f')
        [[ -z "$busid" ]] && continue
        # nvidia-smi reports an 8-hex-digit domain (00000000:a1:00.0); lspci -Dnn
        # uses 4 (0000:a1:00.0). Drop the high 4 digits to match GPU_BDFS.
        norm=$(echo "$busid" | sed -E 's/^[0-9a-f]{4}([0-9a-f]{4}:)/\1/')
        vbios=$(echo "$vbios" | tr -d ' ')
        VBIOS_BY_BDF[$norm]="$vbios"
    done < <(nvidia-smi --query-gpu=pci.bus_id,vbios_version --format=csv,noheader 2>/dev/null || true)
fi

# Full PCIe topology tree (device ordering / enumeration). Enumeration order
# drives PXB-PCIe root-port assignment in chutes-cvm guest launch, which the guest sees as its
# PCI bus layout — another RTMR0 input. No root required.
PCI_TOPOLOGY=$(lspci -tv 2>/dev/null || true)

# ---------------------------------------------------------------------------
# Mellanox / ConnectX NIC detection
# ---------------------------------------------------------------------------
IB_CLASS_DEVICES=()
ETH_CLASS_DEVICES=()
BRIDGE_PFS=()
PASSTHROUGH_CANDIDATES=()
PASSTHROUGH_NUMA_NODES=()

while IFS= read -r line; do
    bdf=$(echo "$line" | awk '{print $1}')
    full_class=$(echo "$line" | grep -oP '\[02[0-9a-fA-F]{2}\]' | tr -d '[]' || true)
    if [[ "$full_class" == "0207" ]]; then
        IB_CLASS_DEVICES+=("$bdf")
    elif [[ "$full_class" == "0200" ]]; then
        ETH_CLASS_DEVICES+=("$bdf")
    fi
done < <(lspci -Dnn | grep '15b3' || true)

# Identify bridge PFs (SMDL=SW_MNG in VPD)
if [[ ${#IB_CLASS_DEVICES[@]} -gt 0 ]]; then
    for bdf in "${IB_CLASS_DEVICES[@]}"; do
        if lspci -vv -s "$bdf" 2>/dev/null | grep -q 'SMDL=SW_MNG' 2>/dev/null; then
            BRIDGE_PFS+=("$bdf")
        else
            PASSTHROUGH_CANDIDATES+=("$bdf")
            # Diagnostic: host NUMA node per IB PF (feeds RTMR0 when IB is passed).
            PASSTHROUGH_NUMA_NODES+=("$(pci_numa_node "$bdf")")
        fi
    done
fi

# ---------------------------------------------------------------------------
# NVSwitch detection
# ---------------------------------------------------------------------------
NVSWITCH_DEVICES=()
NVSWITCH_NUMA_NODES=()
while IFS= read -r line; do
    bdf=$(echo "$line" | awk '{print $1}')
    NVSWITCH_DEVICES+=("$bdf")
    # NVSwitch host NUMA node — drives PXB grouping, so feeds RTMR0.
    NVSWITCH_NUMA_NODES+=("$(pci_numa_node "$bdf")")
done < <(lspci -Dnn | grep '\[0680\]' | grep '10de' || true)

# ---------------------------------------------------------------------------
# Terminal report
# ---------------------------------------------------------------------------
if [[ $REPORT_OUTPUT -eq 1 ]]; then
    HOSTNAME_STR=$(hostname)
    echo ""
    printf '\033[1;37m  GPU Hardware Discovery Report — %s\033[0m\n' "$HOSTNAME_STR"
    bar

    section "Host / Firmware Identity"
    row "Product"        "${PRODUCT_NAME:-(unknown)}"
    row "Baseboard"      "${BOARD_VENDOR:-?} ${BOARD_NAME:-?}"
    row "BIOS"           "${BIOS_VENDOR:-?} ${BIOS_VERSION:-?} (${BIOS_DATE:-?})"
    row "OS VERSION_ID"  "${OS_VERSION_ID:-(unknown)}"

    section "GPUs"
    row "Count"             "$GPU_COUNT"
    row "Unique device IDs" "${UNIQUE_DEVICE_IDS[*]:-none}"
    echo ""
    for i in "${!GPU_BDFS[@]}"; do
        row "  GPU $i: ${GPU_BDFS[$i]}" "device=${GPU_DEVICE_IDS[$i]:-?}  numa=${GPU_NUMA_NODES[$i]}  vbios=${VBIOS_BY_BDF[${GPU_BDFS[$i]}]:-(n/a)}"
    done
    echo ""
    row "BAR size (Region 2)"       "${BAR_SIZE_MB:-(not detected)} MB"
    row "VRAM per GPU (nvidia-smi)" "${VRAM_GB:-(not detected)} GB"
    row "Suggested ram_per_gpu_gb"  "${SUGGESTED_RAM_PER_GPU} GB  (${GPU_COUNT}× = $(( GPU_COUNT * SUGGESTED_RAM_PER_GPU )) GB total)"
    if [[ -n "$GPU_PCI_BARS_SNIPPET" ]]; then
        echo ""
        echo "  Passthrough layout — paste into the GpuProfile subclass:"
        echo "    passthrough = {"
        echo "        \"gpu\": PassthroughDevice(0x10DE, \"${GPU_DEVICE_IDS[0]:-????}\", 0x0302, ["
        printf '%s' "$GPU_PCI_BARS_SNIPPET"
        echo "        ]),"
        echo "        # + \"nvswitch\"/\"ib\" entries if this host passes them through (capture their BARs the same way)"
        echo "    }"
    fi

    section "CPU"
    row "Total CPUs"        "$CPU_TOTAL"
    row "Sockets"           "$CPU_SOCKETS"
    row "Cores per socket"  "$CPU_CORES_PER_SOCKET"
    row "Threads per core"  "$CPU_THREADS_PER_CORE"
    row "Vendor (cpu_vendor)"             "$CPU_VENDOR"
    row "Processor ID (cpu_processor_id)" "$CPU_PROCESSOR_ID"

    section "Memory"
    row "Total host RAM"           "${MEM_TOTAL_GB} GB"
    row "Suggested total VM RAM"   "$(( GPU_COUNT * SUGGESTED_RAM_PER_GPU )) GB"

    section "NUMA"
    row "NUMA node count" "$NUMA_NODE_COUNT"
    for n in "${NUMA_NODES[@]:-}"; do
        row "  Node ${n} CPUs" "${NUMA_CPULIST[$n]}"
    done
    echo ""
    echo "  GPU → NUMA node mapping:"
    for i in "${!GPU_BDFS[@]}"; do
        row "    ${GPU_BDFS[$i]}" "node ${GPU_NUMA_NODES[$i]}"
    done

    section "Launch Determinism (RTMR0-relevant)"
    echo "  These values shape the guest memory map / ACPI tables that extend RTMR0."
    echo "  Diff this section between two hosts to isolate an RTMR0 divergence."
    echo ""
    row "QEMU version"              "${QEMU_VERSION_FULL:-(qemu-system-x86_64 not found)}"
    row "NUMA node count"           "$NUMA_NODE_COUNT"
    row "Guest NUMA topology"       "$([[ "$NUMA_TOPOLOGY_ELIGIBLE" == "yes" ]] \
                                        && echo "ACTIVE (host has 2 nodes) → per-node memory backends + PXB-PCIe" \
                                        || echo "FALLBACK (host has ${NUMA_NODE_COUNT} nodes, not 2) → flat map + numactl interleave")"
    row "QEMU -cpu args"            "$CPU_ARGS"
    row "Host CPU topology"         "sockets=${CPU_SOCKETS}, cores/socket=${CPU_CORES_PER_SOCKET}, threads/core=${CPU_THREADS_PER_CORE}"
    if [[ "$NUMA_TOPOLOGY_ELIGIBLE" == "yes" ]]; then
        warn "Guest RAM (mem=GPU_count × profile.ram_per_gpu_gb) is profile-derived;"
        warn "this script is profile-free — read it from chutes-cvm guest launch's launch log to confirm."
    fi

    section "Mellanox / InfiniBand NICs"
    row "IB-class [0207] devices"   "${#IB_CLASS_DEVICES[@]}"
    row "Ethernet-class [0200]"     "${#ETH_CLASS_DEVICES[@]}"
    row "Bridge PFs (SMDL=SW_MNG)"  "${BRIDGE_PFS[*]:-none}"
    row "IB passthrough candidates" "${PASSTHROUGH_CANDIDATES[*]:-none}"
    row "IB passthrough NUMA nodes" "${PASSTHROUGH_NUMA_NODES[*]:-none}"

    section "NVSwitches"
    row "NVSwitch devices" "${#NVSWITCH_DEVICES[@]}  (${NVSWITCH_DEVICES[*]:-none})"
    row "NVSwitch NUMA nodes" "${NVSWITCH_NUMA_NODES[*]:-none}"

    echo ""
fi

# ---------------------------------------------------------------------------
# JSON output
# ---------------------------------------------------------------------------
if [[ $JSON_OUTPUT -eq 1 ]]; then
    OUT_FILE="discover-profile-$(hostname)-$(date +%Y%m%dT%H%M%S).json"

    json_str_array() {
        local _var="$1"; shift
        local _arr=("$@")
        local _out="["
        local _sep=""
        for _e in "${_arr[@]}"; do
            _out+="${_sep}\"$(printf '%s' "$_e" | sed 's/\\/\\\\/g; s/"/\\"/g')\""
            _sep=", "
        done
        _out+="]"
        printf -v "$_var" '%s' "$_out"
    }

    json_int_array() {
        local _var="$1"; shift
        local _arr=("$@")
        local _out="["
        local _sep=""
        for _e in "${_arr[@]}"; do
            _out+="${_sep}${_e}"
            _sep=", "
        done
        _out+="]"
        printf -v "$_var" '%s' "$_out"
    }

    # Escape an arbitrary string for embedding as a JSON string value.
    # lspci -tv draws the tree with backslashes (\-) and spans multiple lines,
    # so backslash, quote, and control chars must all be escaped.
    json_escape() {
        local _var="$1" s="$2"
        s="${s//\\/\\\\}"
        s="${s//\"/\\\"}"
        s="${s//$'\t'/\\t}"
        s="${s//$'\r'/}"
        s="${s//$'\n'/\\n}"
        printf -v "$_var" '%s' "$s"
    }

    if [[ ${#GPU_BDFS[@]} -gt 0 ]]; then
        json_str_array gpu_bdfs_json "${GPU_BDFS[@]}"
        json_str_array gpu_ids_json  "${GPU_DEVICE_IDS[@]}"
        json_int_array gpu_numa_json "${GPU_NUMA_NODES[@]}"
        gpu_vbios_ordered=()
        for _b in "${GPU_BDFS[@]}"; do
            gpu_vbios_ordered+=("${VBIOS_BY_BDF[$_b]:-}")
        done
        json_str_array gpu_vbios_json "${gpu_vbios_ordered[@]}"
    else
        gpu_bdfs_json="[]"; gpu_ids_json="[]"; gpu_numa_json="[]"; gpu_vbios_json="[]"
    fi

    if [[ -n "$PCI_TOPOLOGY" ]]; then
        json_escape _pci_topo_esc "$PCI_TOPOLOGY"
        pci_topology_json="\"${_pci_topo_esc}\""
    else
        pci_topology_json="null"
    fi
    numa_eligible_json=$([[ "$NUMA_TOPOLOGY_ELIGIBLE" == "yes" ]] && echo 'true' || echo 'false')

    json_escape board_vendor_esc  "$BOARD_VENDOR"
    json_escape board_name_esc    "$BOARD_NAME"
    json_escape bios_vendor_esc   "$BIOS_VENDOR"
    json_escape bios_version_esc  "$BIOS_VERSION"
    json_escape bios_date_esc     "$BIOS_DATE"
    json_escape product_name_esc  "$PRODUCT_NAME"
    json_escape os_version_esc    "$OS_VERSION_ID"
    json_escape cpu_args_esc      "$CPU_ARGS"
    json_escape cpu_vendor_esc    "$CPU_VENDOR"
    # cpu_processor_id is null when a field was unreadable.
    if [[ -n "$CPU_PROCESSOR_ID" ]]; then
        json_escape _pid_esc "$CPU_PROCESSOR_ID"
        cpu_processor_id_json="\"${_pid_esc}\""
    else
        cpu_processor_id_json="null"
    fi
    json_escape qemu_version_esc      "$QEMU_VERSION"
    json_escape qemu_version_full_esc "$QEMU_VERSION_FULL"

    if [[ ${#UNIQUE_DEVICE_IDS[@]} -gt 0 ]]; then
        json_str_array uniq_ids_json "${UNIQUE_DEVICE_IDS[@]}"
    else
        uniq_ids_json="[]"
    fi

    numa_nodes_json="["
    numa_cpus_json="{"
    _sep=""
    for n in "${NUMA_NODES[@]:-}"; do
        numa_nodes_json+="${_sep}${n}"
        numa_cpus_json+="${_sep}\"${n}\": \"${NUMA_CPULIST[$n]}\""
        _sep=", "
    done
    numa_nodes_json+="]"
    numa_cpus_json+="}"

    if [[ ${#IB_CLASS_DEVICES[@]} -gt 0 ]]; then
        json_str_array ib_json "${IB_CLASS_DEVICES[@]}"
    else
        ib_json="[]"
    fi

    if [[ ${#BRIDGE_PFS[@]} -gt 0 ]]; then
        json_str_array bridge_json "${BRIDGE_PFS[@]}"
    else
        bridge_json="[]"
    fi

    if [[ ${#PASSTHROUGH_CANDIDATES[@]} -gt 0 ]]; then
        json_str_array passthru_json "${PASSTHROUGH_CANDIDATES[@]}"
        json_int_array passthru_numa_json "${PASSTHROUGH_NUMA_NODES[@]}"
    else
        passthru_json="[]"; passthru_numa_json="[]"
    fi

    if [[ ${#NVSWITCH_DEVICES[@]} -gt 0 ]]; then
        json_str_array nvswitch_json "${NVSWITCH_DEVICES[@]}"
        json_int_array nvswitch_numa_json "${NVSWITCH_NUMA_NODES[@]}"
    else
        nvswitch_json="[]"; nvswitch_numa_json="[]"
    fi

    cat > "$OUT_FILE" <<JSON
{
  "hostname": "$(hostname)",
  "timestamp": "$(date -u +%Y-%m-%dT%H:%M:%SZ)",
  "host": {
    "product_name": "${product_name_esc}",
    "board_vendor": "${board_vendor_esc}",
    "board_name": "${board_name_esc}",
    "bios_vendor": "${bios_vendor_esc}",
    "bios_version": "${bios_version_esc}",
    "bios_date": "${bios_date_esc}",
    "os_version_id": "${os_version_esc}"
  },
  "launch_determinism": {
    "qemu_version": "${qemu_version_esc}",
    "qemu_version_full": "${qemu_version_full_esc}",
    "numa_node_count": ${NUMA_NODE_COUNT},
    "numa_topology_eligible": ${numa_eligible_json},
    "cpu_args": "${cpu_args_esc}",
    "host_cpu_topology": "sockets=${CPU_SOCKETS},cores_per_socket=${CPU_CORES_PER_SOCKET},threads_per_core=${CPU_THREADS_PER_CORE}"
  },
  "gpu": {
    "pci_device_ids": ${uniq_ids_json},
    "bdfs": ${gpu_bdfs_json},
    "count": ${GPU_COUNT},
    "vram_gb": ${VRAM_GB:-null},
    "bar_size_mb": ${BAR_SIZE_MB:--1},
    "numa_nodes": ${gpu_numa_json},
    "vbios": ${gpu_vbios_json}
  },
  "pci_topology": ${pci_topology_json},
  "cpu": {
    "total": ${CPU_TOTAL},
    "sockets": ${CPU_SOCKETS},
    "cores_per_socket": ${CPU_CORES_PER_SOCKET},
    "threads_per_core": ${CPU_THREADS_PER_CORE},
    "cpu_vendor": "${cpu_vendor_esc}",
    "cpu_processor_id": ${cpu_processor_id_json}
  },
  "memory": {
    "total_gb": ${MEM_TOTAL_GB},
    "suggested_ram_per_gpu_gb": ${SUGGESTED_RAM_PER_GPU},
    "suggested_total_vm_ram_gb": $(( GPU_COUNT * SUGGESTED_RAM_PER_GPU ))
  },
  "numa": {
    "node_count": ${NUMA_NODE_COUNT},
    "nodes": ${numa_nodes_json},
    "cpus_per_node": ${numa_cpus_json}
  },
  "nic": {
    "ib_class_count": ${#IB_CLASS_DEVICES[@]},
    "eth_class_count": ${#ETH_CLASS_DEVICES[@]},
    "ib_devices": ${ib_json},
    "bridge_pfs": ${bridge_json},
    "passthrough_candidates": ${passthru_json},
    "passthrough_numa_nodes": ${passthru_numa_json}
  },
  "nvswitch": {
    "present": $( [[ ${#NVSWITCH_DEVICES[@]} -gt 0 ]] && echo 'true' || echo 'false' ),
    "count": ${#NVSWITCH_DEVICES[@]},
    "devices": ${nvswitch_json},
    "numa_nodes": ${nvswitch_numa_json}
  }
}
JSON

    if [[ $REPORT_OUTPUT -eq 1 ]]; then
        printf '  JSON written to: %s\n\n' "$OUT_FILE"
    else
        echo "$OUT_FILE"
    fi
fi
