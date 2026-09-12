package kubernetes.admission

import future.keywords.contains
import future.keywords.if
import future.keywords.in

import data.helpers

# =============================================================================
# CAPABILITY RESTRICTIONS
# =============================================================================

deny contains msg if {
    input.request.operation in ["CREATE", "UPDATE"]
    not helpers.is_rollout_restart
    helpers.is_pod_resource
    not helpers.is_system_or_controller_user
    
    input.request.kind.kind == "Pod"
    container := input.request.object.spec.containers[_]
    has_dangerous_capability(container)
    msg := sprintf("Container '%s' requests dangerous capability", [container.name])
}

deny contains msg if {
    input.request.operation in ["CREATE", "UPDATE"]
    not helpers.is_rollout_restart
    helpers.is_pod_resource
    not helpers.is_system_or_controller_user
    
    input.request.kind.kind == "Pod"
    container := input.request.object.spec.initContainers[_]
    has_dangerous_capability(container)
    msg := sprintf("Init container '%s' requests dangerous capability", [container.name])
}

deny contains msg if {
    input.request.operation in ["CREATE", "UPDATE"]
    not helpers.is_rollout_restart
    helpers.is_pod_resource
    not helpers.is_system_or_controller_user
    
    input.request.kind.kind == "Pod"
    container := input.request.object.spec.ephemeralContainers[_]
    has_dangerous_capability(container)
    msg := sprintf("Ephemeral container '%s' requests dangerous capability", [container.name])
}

deny contains msg if {
    input.request.operation in ["CREATE", "UPDATE"]
    not helpers.is_rollout_restart
    helpers.is_pod_resource
    not helpers.is_system_or_controller_user
    not only_restartedAt_change

    input.request.kind.kind in ["Deployment", "StatefulSet", "DaemonSet", "ReplicaSet", "Job"]
    container := input.request.object.spec.template.spec.containers[_]
    has_dangerous_capability(container)
    msg := sprintf("Container '%s' requests dangerous capability", [container.name])
}

deny contains msg if {
    input.request.operation in ["CREATE", "UPDATE"]
    not helpers.is_rollout_restart
    helpers.is_pod_resource
    not helpers.is_system_or_controller_user
    not only_restartedAt_change

    input.request.kind.kind in ["Deployment", "StatefulSet", "DaemonSet", "ReplicaSet", "Job"]
    container := input.request.object.spec.template.spec.initContainers[_]
    has_dangerous_capability(container)
    msg := sprintf("Init container '%s' requests dangerous capability", [container.name])
}

deny contains msg if {
    input.request.operation in ["CREATE", "UPDATE"]
    not helpers.is_rollout_restart
    helpers.is_pod_resource
    not helpers.is_system_or_controller_user
    not only_restartedAt_change

    input.request.kind.kind in ["Deployment", "StatefulSet", "DaemonSet", "ReplicaSet", "Job"]
    container := input.request.object.spec.template.spec.ephemeralContainers[_]
    has_dangerous_capability(container)
    msg := sprintf("Ephemeral container '%s' requests dangerous capability", [container.name])
}

deny contains msg if {
    input.request.operation in ["CREATE", "UPDATE"]
    not helpers.is_rollout_restart
    helpers.is_pod_resource
    not helpers.is_system_or_controller_user
    
    input.request.kind.kind == "CronJob"
    container := input.request.object.spec.jobTemplate.spec.template.spec.containers[_]
    has_dangerous_capability(container)
    msg := sprintf("Container '%s' requests dangerous capability", [container.name])
}

deny contains msg if {
    input.request.operation in ["CREATE", "UPDATE"]
    not helpers.is_rollout_restart
    helpers.is_pod_resource
    not helpers.is_system_or_controller_user
    
    input.request.kind.kind == "CronJob"
    container := input.request.object.spec.jobTemplate.spec.template.spec.initContainers[_]
    has_dangerous_capability(container)
    msg := sprintf("Init container '%s' requests dangerous capability", [container.name])
}

deny contains msg if {
    input.request.operation in ["CREATE", "UPDATE"]
    not helpers.is_rollout_restart
    helpers.is_pod_resource
    not helpers.is_system_or_controller_user
    
    input.request.kind.kind == "CronJob"
    container := input.request.object.spec.jobTemplate.spec.template.spec.ephemeralContainers[_]
    has_dangerous_capability(container)
    msg := sprintf("Ephemeral container '%s' requests dangerous capability", [container.name])
}

# Capabilities a workload may request. An ALLOWLIST, not a denylist: the previous 8-name denylist
# admitted every capability outside it -- DAC_READ_SEARCH, DAC_OVERRIDE, NET_ADMIN, SETUID,
# SYS_RESOURCE, LINUX_IMMUTABLE -- and admitted the literal "ALL" as well, since "ALL" is not a
# capability name and so never matched. That matters because the user-workload seccomp profile is
# SCMP_ACT_ALLOW by default with a 22-syscall denylist: it blocks mount(2) but not the newer
# fsopen/fsconfig/fsmount/move_mount/open_tree, and does not block name_to_handle_at or
# open_by_handle_at. So DAC_READ_SEARCH alone reads arbitrary inodes on the guest root, and
# SYS_ADMIN reconstitutes mounting around the blocked syscall.
#
#   IPC_LOCK          -- chute containers lock model weights into memory
#   NET_BIND_SERVICE  -- bind below port 1024; the sole addition Kubernetes' restricted PSS permits
allowed_capabilities := {"IPC_LOCK", "NET_BIND_SERVICE"}

# Kubernetes accepts "SYS_ADMIN", "CAP_SYS_ADMIN" and lowercase spellings, and containerd
# normalises before applying them. Compare on the canonical bare-uppercase form so no spelling
# reaches the container unchecked.
normalized_capability(cap) := trim_prefix(upper(cap), "CAP_")

has_dangerous_capability(container) if {
    cap := container.securityContext.capabilities.add[_]
    not normalized_capability(cap) in allowed_capabilities
}


# =============================================================================
# SECURITY CONTEXT RESTRICTIONS
# =============================================================================

deny contains msg if {
    input.request.operation in ["CREATE", "UPDATE"]
    not helpers.is_rollout_restart
    helpers.is_pod_resource
    not helpers.is_system_or_controller_user
    
    input.request.kind.kind == "Pod"
    container := input.request.object.spec.containers[_]
    container.securityContext.privileged == true
    msg := sprintf("Container '%s' has privileged security context", [container.name])
}

deny contains msg if {
    input.request.operation in ["CREATE", "UPDATE"]
    not helpers.is_rollout_restart
    helpers.is_pod_resource
    not helpers.is_system_or_controller_user
    
    input.request.kind.kind == "Pod"
    input.request.object.spec.hostNetwork == true
    msg := "Pod uses host network which is not allowed"
}

deny contains msg if {
    input.request.operation in ["CREATE", "UPDATE"]
    not helpers.is_rollout_restart
    helpers.is_pod_resource
    not helpers.is_system_or_controller_user
    not only_restartedAt_change

    input.request.kind.kind in ["Deployment", "StatefulSet", "DaemonSet", "ReplicaSet", "Job"]
    input.request.object.spec.template.spec.hostNetwork == true
    msg := "Template uses host network which is not allowed"
}

deny contains msg if {
    input.request.operation in ["CREATE", "UPDATE"]
    not helpers.is_rollout_restart
    helpers.is_pod_resource
    not helpers.is_system_or_controller_user
    
    input.request.kind.kind == "CronJob"
    input.request.object.spec.jobTemplate.spec.template.spec.hostNetwork == true
    msg := "CronJob template uses host network which is not allowed"
}

deny contains msg if {
    input.request.operation in ["CREATE", "UPDATE"]
    not helpers.is_rollout_restart
    helpers.is_pod_resource
    not helpers.is_system_or_controller_user
    
    input.request.kind.kind == "Pod"
    input.request.object.spec.hostPID == true
    msg := "Pod uses host PID namespace which is not allowed"
}

deny contains msg if {
    input.request.operation in ["CREATE", "UPDATE"]
    not helpers.is_rollout_restart
    helpers.is_pod_resource
    not helpers.is_system_or_controller_user
    not only_restartedAt_change

    input.request.kind.kind in ["Deployment", "StatefulSet", "DaemonSet", "ReplicaSet", "Job"]
    input.request.object.spec.template.spec.hostPID == true
    msg := "Template uses host PID namespace which is not allowed"
}

deny contains msg if {
    input.request.operation in ["CREATE", "UPDATE"]
    not helpers.is_rollout_restart
    helpers.is_pod_resource
    not helpers.is_system_or_controller_user
    
    input.request.kind.kind == "CronJob"
    input.request.object.spec.jobTemplate.spec.template.spec.hostPID == true
    msg := "CronJob template uses host PID namespace which is not allowed"
}

deny contains msg if {
    input.request.operation in ["CREATE", "UPDATE"]
    not helpers.is_rollout_restart
    helpers.is_pod_resource
    not helpers.is_system_or_controller_user
    
    input.request.kind.kind == "Pod"
    input.request.object.spec.hostIPC == true
    msg := "Pod uses host IPC namespace which is not allowed"
}

deny contains msg if {
    input.request.operation in ["CREATE", "UPDATE"]
    not helpers.is_rollout_restart
    helpers.is_pod_resource
    not helpers.is_system_or_controller_user
    not only_restartedAt_change

    input.request.kind.kind in ["Deployment", "StatefulSet", "DaemonSet", "ReplicaSet", "Job"]
    input.request.object.spec.template.spec.hostIPC == true
    msg := "Template uses host IPC namespace which is not allowed"
}

deny contains msg if {
    input.request.operation in ["CREATE", "UPDATE"]
    not helpers.is_rollout_restart
    helpers.is_pod_resource
    not helpers.is_system_or_controller_user
    
    input.request.kind.kind == "CronJob"
    input.request.object.spec.jobTemplate.spec.template.spec.hostIPC == true
    msg := "CronJob template uses host IPC namespace which is not allowed"
}

deny contains msg if {
    input.request.operation in ["CREATE", "UPDATE"]
    not helpers.is_rollout_restart
    helpers.is_pod_resource
    not helpers.is_system_or_controller_user
    
    input.request.kind.kind == "Pod"
    container := input.request.object.spec.containers[_]
    container.securityContext.allowPrivilegeEscalation == true
    msg := sprintf("Container '%s' allows privilege escalation", [container.name])
}

deny contains msg if {
    input.request.operation in ["CREATE", "UPDATE"]
    not helpers.is_rollout_restart
    helpers.is_pod_resource
    not helpers.is_system_or_controller_user
    
    input.request.kind.kind == "Pod"
    container := input.request.object.spec.initContainers[_]
    container.securityContext.allowPrivilegeEscalation == true
    msg := sprintf("Init container '%s' allows privilege escalation", [container.name])
}

deny contains msg if {
    input.request.operation in ["CREATE", "UPDATE"]
    not helpers.is_rollout_restart
    helpers.is_pod_resource
    not helpers.is_system_or_controller_user
    
    input.request.kind.kind == "Pod"
    container := input.request.object.spec.ephemeralContainers[_]
    container.securityContext.allowPrivilegeEscalation == true
    msg := sprintf("Ephemeral container '%s' allows privilege escalation", [container.name])
}

deny contains msg if {
    input.request.operation in ["CREATE", "UPDATE"]
    not helpers.is_rollout_restart
    helpers.is_pod_resource
    not helpers.is_system_or_controller_user
    not only_restartedAt_change

    input.request.kind.kind in ["Deployment", "StatefulSet", "DaemonSet", "ReplicaSet", "Job"]
    container := input.request.object.spec.template.spec.containers[_]
    container.securityContext.allowPrivilegeEscalation == true
    msg := sprintf("Container '%s' allows privilege escalation", [container.name])
}

deny contains msg if {
    input.request.operation in ["CREATE", "UPDATE"]
    not helpers.is_rollout_restart
    helpers.is_pod_resource
    not helpers.is_system_or_controller_user
    not only_restartedAt_change

    input.request.kind.kind in ["Deployment", "StatefulSet", "DaemonSet", "ReplicaSet", "Job"]
    container := input.request.object.spec.template.spec.initContainers[_]
    container.securityContext.allowPrivilegeEscalation == true
    msg := sprintf("Init container '%s' allows privilege escalation", [container.name])
}

deny contains msg if {
    input.request.operation in ["CREATE", "UPDATE"]
    not helpers.is_rollout_restart
    helpers.is_pod_resource
    not helpers.is_system_or_controller_user
    not only_restartedAt_change

    input.request.kind.kind in ["Deployment", "StatefulSet", "DaemonSet", "ReplicaSet", "Job"]
    container := input.request.object.spec.template.spec.ephemeralContainers[_]
    container.securityContext.allowPrivilegeEscalation == true
    msg := sprintf("Ephemeral container '%s' allows privilege escalation", [container.name])
}

deny contains msg if {
    input.request.operation in ["CREATE", "UPDATE"]
    not helpers.is_rollout_restart
    helpers.is_pod_resource
    not helpers.is_system_or_controller_user
    
    input.request.kind.kind == "CronJob"
    container := input.request.object.spec.jobTemplate.spec.template.spec.containers[_]
    container.securityContext.allowPrivilegeEscalation == true
    msg := sprintf("Container '%s' allows privilege escalation", [container.name])
}

deny contains msg if {
    input.request.operation in ["CREATE", "UPDATE"]
    not helpers.is_rollout_restart
    helpers.is_pod_resource
    not helpers.is_system_or_controller_user
    
    input.request.kind.kind == "CronJob"
    container := input.request.object.spec.jobTemplate.spec.template.spec.initContainers[_]
    container.securityContext.allowPrivilegeEscalation == true
    msg := sprintf("Init container '%s' allows privilege escalation", [container.name])
}

deny contains msg if {
    input.request.operation in ["CREATE", "UPDATE"]
    not helpers.is_rollout_restart
    helpers.is_pod_resource
    not helpers.is_system_or_controller_user
    
    input.request.kind.kind == "CronJob"
    container := input.request.object.spec.jobTemplate.spec.template.spec.ephemeralContainers[_]
    container.securityContext.allowPrivilegeEscalation == true
    msg := sprintf("Ephemeral container '%s' allows privilege escalation", [container.name])
}

# Deny pods with privileged containers
deny contains msg if {
    input.request.operation in ["CREATE", "UPDATE"]
    not helpers.is_rollout_restart
    # Check containers in pod spec
    not helpers.is_system_or_controller_user
    container := input.request.object.spec.containers[_]
    container.securityContext.privileged == true
    
    msg := sprintf("Container '%s' has privileged security context which is not allowed", [container.name])
}

deny contains msg if {
    input.request.operation in ["CREATE", "UPDATE"]
    not helpers.is_rollout_restart
    # Check init containers in pod spec
    not helpers.is_system_or_controller_user
    container := input.request.object.spec.initContainers[_]
    container.securityContext.privileged == true
    
    msg := sprintf("Init container '%s' has privileged security context which is not allowed", [container.name])
}

deny contains msg if {
    input.request.operation in ["CREATE", "UPDATE"]
    not helpers.is_rollout_restart
    # Check ephemeral containers in pod spec
    not helpers.is_system_or_controller_user
    container := input.request.object.spec.ephemeralContainers[_]
    container.securityContext.privileged == true
    
    msg := sprintf("Ephemeral container '%s' has privileged security context which is not allowed", [container.name])
}

deny contains msg if {
    input.request.operation in ["CREATE", "UPDATE"]
    not helpers.is_rollout_restart
    # Check containers in deployment/replicaset/etc template
    not helpers.is_system_or_controller_user
    not only_restartedAt_change
    container := input.request.object.spec.template.spec.containers[_]
    container.securityContext.privileged == true
    msg := sprintf("Container '%s' has privileged security context which is not allowed", [container.name])
}

deny contains msg if {
    input.request.operation in ["CREATE", "UPDATE"]
    not helpers.is_rollout_restart
    # Check init containers in deployment/replicaset/etc template
    not helpers.is_system_or_controller_user
    not only_restartedAt_change
    container := input.request.object.spec.template.spec.initContainers[_]
    container.securityContext.privileged == true
    msg := sprintf("Init container '%s' has privileged security context which is not allowed", [container.name])
}

deny contains msg if {
    input.request.operation in ["CREATE", "UPDATE"]
    not helpers.is_rollout_restart
    # Check ephemeral containers in deployment/replicaset/etc template
    not helpers.is_system_or_controller_user
    not only_restartedAt_change
    container := input.request.object.spec.template.spec.ephemeralContainers[_]
    container.securityContext.privileged == true
    msg := sprintf("Ephemeral container '%s' has privileged security context which is not allowed", [container.name])
}

deny contains msg if {
    input.request.operation in ["CREATE", "UPDATE"]
    not helpers.is_rollout_restart
    not helpers.is_system_or_controller_user
    input.request.kind.kind == "CronJob"
    container := input.request.object.spec.jobTemplate.spec.template.spec.containers[_]
    container.securityContext.privileged == true
    
    msg := sprintf("Container '%s' has privileged security context which is not allowed", [container.name])
}

deny contains msg if {
    input.request.operation in ["CREATE", "UPDATE"]
    not helpers.is_rollout_restart
    not helpers.is_system_or_controller_user
    input.request.kind.kind == "CronJob"
    container := input.request.object.spec.jobTemplate.spec.template.spec.initContainers[_]
    container.securityContext.privileged == true
    
    msg := sprintf("Init container '%s' has privileged security context which is not allowed", [container.name])
}

deny contains msg if {
    input.request.operation in ["CREATE", "UPDATE"]
    not helpers.is_rollout_restart
    not helpers.is_system_or_controller_user
    input.request.kind.kind == "CronJob"
    container := input.request.object.spec.jobTemplate.spec.template.spec.ephemeralContainers[_]
    container.securityContext.privileged == true
    
    msg := sprintf("Ephemeral container '%s' has privileged security context which is not allowed", [container.name])
}

# =============================================================================
# RESOURCE LIMITS
# =============================================================================

deny contains msg if {
    input.request.operation in ["CREATE", "UPDATE"]
    not helpers.is_rollout_restart
    helpers.is_pod_resource
    not helpers.is_system_or_controller_user
    
    input.request.kind.kind == "Pod"
    container := input.request.object.spec.containers[_]
    not container.resources.limits
    msg := sprintf("Container '%s' missing resource limits", [container.name])
}

deny contains msg if {
    input.request.operation in ["CREATE", "UPDATE"]
    not helpers.is_rollout_restart
    helpers.is_pod_resource
    not helpers.is_system_or_controller_user
    
    input.request.kind.kind == "Pod"
    container := input.request.object.spec.containers[_]
    container.resources.limits
    not container.resources.limits.memory
    msg := sprintf("Container '%s' missing memory limit", [container.name])
}

deny contains msg if {
    input.request.operation in ["CREATE", "UPDATE"]
    not helpers.is_rollout_restart
    helpers.is_pod_resource
    not helpers.is_system_or_controller_user
    not only_restartedAt_change

    input.request.kind.kind in ["Deployment", "StatefulSet", "DaemonSet", "ReplicaSet", "Job"]
    container := input.request.object.spec.template.spec.containers[_]
    not container.resources.limits
    msg := sprintf("Container '%s' missing resource limits", [container.name])
}

deny contains msg if {
    input.request.operation in ["CREATE", "UPDATE"]
    not helpers.is_rollout_restart
    helpers.is_pod_resource
    not helpers.is_system_or_controller_user
    not only_restartedAt_change

    input.request.kind.kind in ["Deployment", "StatefulSet", "DaemonSet", "ReplicaSet", "Job"]
    container := input.request.object.spec.template.spec.containers[_]
    container.resources.limits
    not container.resources.limits.memory
    msg := sprintf("Container '%s' missing memory limit", [container.name])
}

deny contains msg if {
    input.request.operation in ["CREATE", "UPDATE"]
    not helpers.is_rollout_restart
    helpers.is_pod_resource
    not helpers.is_system_or_controller_user
    
    input.request.kind.kind == "CronJob"
    container := input.request.object.spec.jobTemplate.spec.template.spec.containers[_]
    not container.resources.limits
    msg := sprintf("Container '%s' missing resource limits", [container.name])
}

deny contains msg if {
    input.request.operation in ["CREATE", "UPDATE"]
    not helpers.is_rollout_restart
    helpers.is_pod_resource
    not helpers.is_system_or_controller_user
    
    input.request.kind.kind == "CronJob"
    container := input.request.object.spec.jobTemplate.spec.template.spec.containers[_]
    container.resources.limits
    not container.resources.limits.memory
    msg := sprintf("Container '%s' missing memory limit", [container.name])
}

# =============================================================================
# ENVIRONMENT VARIABLE RESTRICTIONS
# =============================================================================

deny contains msg if {
    input.request.operation in ["CREATE", "UPDATE"]
    not helpers.is_rollout_restart
    helpers.is_pod_resource
    not helpers.is_system_or_controller_user
    
    input.request.kind.kind == "Pod"
    container := input.request.object.spec.containers[_]
    env := container.env[_]
    is_forbidden_env_var(env.name)
    msg := sprintf("Container '%s' uses forbidden environment variable '%s'", [container.name, env.name])
}

deny contains msg if {
    input.request.operation in ["CREATE", "UPDATE"]
    not helpers.is_rollout_restart
    helpers.is_pod_resource
    not helpers.is_system_or_controller_user
    not only_restartedAt_change

    input.request.kind.kind in ["Deployment", "StatefulSet", "DaemonSet", "ReplicaSet", "Job"]
    container := input.request.object.spec.template.spec.containers[_]
    env := container.env[_]
    is_forbidden_env_var(env.name)
    msg := sprintf("Container '%s' uses forbidden environment variable '%s'", [container.name, env.name])
}

deny contains msg if {
    input.request.operation in ["CREATE", "UPDATE"]
    not helpers.is_rollout_restart
    helpers.is_pod_resource
    not helpers.is_system_or_controller_user
    
    input.request.kind.kind == "CronJob"
    container := input.request.object.spec.jobTemplate.spec.template.spec.containers[_]
    env := container.env[_]
    is_forbidden_env_var(env.name)
    msg := sprintf("Container '%s' uses forbidden environment variable '%s'", [container.name, env.name])
}

# The three rules above walk spec.containers only. Init and ephemeral containers were omitted, and
# cache-init is the one container permitted to run as uid 0 — so LD_PRELOAD there is root code
# execution on a signed image, using a field the policy never read.
non_main_containers(spec) := array.concat(
    object.get(spec, "initContainers", []),
    object.get(spec, "ephemeralContainers", []),
)

deny contains msg if {
    input.request.operation in ["CREATE", "UPDATE"]
    not helpers.is_rollout_restart
    helpers.is_pod_resource
    not helpers.is_system_or_controller_user

    input.request.kind.kind == "Pod"
    container := non_main_containers(input.request.object.spec)[_]
    env := container.env[_]
    is_forbidden_env_var(env.name)
    msg := sprintf("Container '%s' uses forbidden environment variable '%s'", [container.name, env.name])
}

deny contains msg if {
    input.request.operation in ["CREATE", "UPDATE"]
    not helpers.is_rollout_restart
    helpers.is_pod_resource
    not helpers.is_system_or_controller_user

    input.request.kind.kind in ["Deployment", "StatefulSet", "DaemonSet", "ReplicaSet", "Job"]
    container := non_main_containers(input.request.object.spec.template.spec)[_]
    env := container.env[_]
    is_forbidden_env_var(env.name)
    msg := sprintf("Container '%s' uses forbidden environment variable '%s'", [container.name, env.name])
}

deny contains msg if {
    input.request.operation in ["CREATE", "UPDATE"]
    not helpers.is_rollout_restart
    helpers.is_pod_resource
    not helpers.is_system_or_controller_user

    input.request.kind.kind == "CronJob"
    container := non_main_containers(input.request.object.spec.jobTemplate.spec.template.spec)[_]
    env := container.env[_]
    is_forbidden_env_var(env.name)
    msg := sprintf("Container '%s' uses forbidden environment variable '%s'", [container.name, env.name])
}

# List of forbidden environment variables (customize as needed)
is_forbidden_env_var(name) if {
    name in [
        "KUBECONFIG",
        "KUBE_TOKEN"
    ]
}

is_forbidden_env_var(name) if {
    not name in allowed_env_vars
}

# Allow certain environment variables that are needed
allowed_env_vars := {
    "HF_TOKEN",
    # cache-cleaner init container (chutes-miner build_chute_job)
    "CACHE_MAX_AGE_DAYS",
    "CACHE_MAX_SIZE_GB",
    "CLEANUP_EXCLUDE",
    # downward-API node name, used by chutes-miner chart init containers
    "NODE_NAME",
    "CUDA_VISIBLE_DEVICES",
    "NVIDIA_VISIBLE_DEVICES",
    "CHUTES_NVIDIA_DEVICES",
    "PATH",
    "HOME",
    "USER",
    "LANG",
    "LC_ALL",
    "TZ",
    "MINER_SEED",
    "MINER_SS58",
    "VALIDATORS",
    "CLUSTER_NAME",
    "CONTROL_PLANE_URL_FILE",
    "CHUTES_EXECUTION_CONTEXT",
    "CHUTES_EXTERNAL_HOST",
    "CHUTES_LAUNCH_JWT",
    "CHUTES_PORT_LOGGING",
    "CHUTES_PORT_PRIMARY",
    "CHUTES_PORT_ATTESTATION",
    "HF_HOME",
    "HF_HUB_DISABLE_XET",
    "HF_HUB_ENABLE_HF_TRANSFER",
    "HF_XET_FIXED_DOWNLOAD_CONCURRENCY",
    "TOKIO_WORKER_THREADS",
    "CIVITAI_HOME",
    "NCCL_DEBUG",
    "NCCL_IB_DISABLE",
    "NCCL_NET_GDR_LEVEL",
    "NCCL_P2P_DISABLE",
    "NCCL_SHM_DISABLE",
    "NCCL_SOCKET_FAMILY",
    "NCCL_SOCKET_IFNAME",
    "VLLM_DISABLE_TELEMETRY",
}

# =============================================================================
# EXEC/ATTACH  RESTRICTIONS
# =============================================================================

# Block ALL exec operations
deny contains msg if {
    input.request.kind.kind == "PodExecOptions"
    not helpers.is_system_or_controller_user
    msg := "Pod exec operations are not permitted."
}

# Block ALL attach operations
deny contains msg if {
    input.request.kind.kind == "PodAttachOptions"
    not helpers.is_system_or_controller_user
    msg := "Pod attach operations are not permitted."
}

# Block ALL port forward operations
deny contains msg if {
    input.request.kind.kind == "PodPortForwardOptions"
    not helpers.is_system_or_controller_user
    msg := "Pod port forward operations are not permitted."
}