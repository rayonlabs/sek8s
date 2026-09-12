"""Sample host topologies for tests.

These were formerly ``chutes_cvm.guest.gpu.known_topologies`` — the in-repo baseline registry.
Production no longer hardcodes host classes (the API is the source of truth: measurements are
generated from the host profiles it returns), so these live here purely as fixtures to exercise
the RTMR0 / topology-spec machinery with realistic CpuTopology / TopologyFingerprint values.
"""

from chutes_cvm.guest.gpu.topology import (
    CpuTopology,
    FlatTopology,
    NumaTopology,
    TopologyFingerprint,
)

# ── CPU shapes ──────────────────────────────────────────────────────────────────
H200_EMERALD = CpuTopology(
    vcpus=124, sockets=2, cpu_vendor="GenuineIntel", cpu_processor_id="f2060c00fffba91f"
)
B200_XEON = CpuTopology(vcpus=176, sockets=2, cpu_vendor="GenuineIntel")
B200_XEON6 = CpuTopology(vcpus=272, sockets=2, cpu_vendor="GenuineIntel")
RTX_XEON = CpuTopology(
    vcpus=124, sockets=2, cpu_vendor="GenuineIntel", cpu_processor_id="f3060a00fffba91f"
)

# ── Full fingerprints (CpuTopology × guest RAM × GpuTopology) ────────────────────
H200_KR6288 = TopologyFingerprint(
    H200_EMERALD,
    1128,
    NumaTopology(gpu_nodes=(0, 0, 0, 0, 1, 1, 1, 1), nvswitch_nodes=(0, 0, 0, 0)),
)
H200_XE9680 = TopologyFingerprint(
    H200_EMERALD,
    1128,
    NumaTopology(gpu_nodes=(0, 0, 0, 0, 1, 1, 1, 1), nvswitch_nodes=(1, 1, 1, 1)),
)
B200_XEON_FP = TopologyFingerprint(
    B200_XEON, 1944, NumaTopology(gpu_nodes=(0, 0, 0, 0, 1, 1, 1, 1))
)
B200_XEON6_FP = TopologyFingerprint(
    B200_XEON6, 2952, NumaTopology(gpu_nodes=(0, 0, 0, 0, 1, 1, 1, 1))
)
RTX_NUMA = TopologyFingerprint(
    RTX_XEON, 768, NumaTopology(gpu_nodes=(0, 0, 0, 0, 1, 1, 1, 1))
)
RTX_FLAT = TopologyFingerprint(RTX_XEON, 768, FlatTopology(gpu_count=8))
