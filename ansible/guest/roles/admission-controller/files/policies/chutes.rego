package kubernetes.admission

import future.keywords.contains
import future.keywords.if
import future.keywords.in

import data.helpers

# =============================================================================
# CHUTES NAMESPACE: NO ROOT / NO SUDO
# =============================================================================
# Pod-spec rules (root, runAsUser, runAsNonRoot, command) apply on CREATE for all
# resources, and on UPDATE for higher-level resources whose template spec is mutable.
# Pod specs are immutable after creation so UPDATE validation is redundant and would
# block operations (e.g. finalizer removal) on pre-existing pods.
chutes_apply_pod_spec_rules if {
	input.request.operation == "CREATE"
}

chutes_apply_pod_spec_rules if {
	input.request.operation == "UPDATE"
	input.request.kind.kind != "Pod"
}

# In chutes namespace no pod/container may run as root (UID 0). There is exactly
# one exception: the init container named "cache-init" with image parachutes/cache-cleaner*
# may run as root to chmod the hostPath cache dir that kubelet may create as root.
# To allow root anywhere else, the only place to change is chutes_is_cache_cleaner_init
# and the logic in chutes_container_runs_root_denied below.

# ONLY root exception: cache-init with parachutes/cache-cleaner (name + image).
# Image from Docker Hub is e.g. parachutes/cache-cleaner:release-next-latest (no docker.io prefix in spec).
chutes_is_cache_cleaner_init(container) if {
	container.name == "cache-init"
	regex.match("^parachutes/cache-cleaner[:@]", container.image)
}

# Effective runAsUser for a container: container override or pod-level default
chutes_effective_run_as_user(container, pod_spec) := uid if {
	uid := container.securityContext.runAsUser
}

chutes_effective_run_as_user(container, pod_spec) := uid if {
	not container.securityContext.runAsUser
	uid := pod_spec.securityContext.runAsUser
}

# True when this container runs as root and that is not allowed (deny).
# Single place for root-denial logic: main/ephemeral always denied if root;
# init denied if root unless chutes_is_cache_cleaner_init(container).
chutes_container_runs_root_denied(container, pod_spec, is_init_container) if {
	chutes_effective_run_as_user(container, pod_spec) == 0
	not is_init_container
}

chutes_container_runs_root_denied(container, pod_spec, is_init_container) if {
	chutes_effective_run_as_user(container, pod_spec) == 0
	is_init_container
	not chutes_is_cache_cleaner_init(container)
}

# Deny chutes namespace if pod-level runAsUser is root
deny contains msg if {
	chutes_apply_pod_spec_rules
	input.request.namespace == "chutes"
	input.request.kind.kind == "Pod"
	helpers.is_pod_resource
	input.request.object.spec.securityContext.runAsUser == 0
	msg := "Chutes namespace: pods must not run as root (runAsUser: 0)"
}

# Deny when pod-level runAsUser is unspecified (would allow image default, often root)
deny contains msg if {
	chutes_apply_pod_spec_rules
	input.request.namespace == "chutes"
	input.request.kind.kind == "Pod"
	helpers.is_pod_resource
	not input.request.object.spec.securityContext.runAsUser
	msg := "Chutes namespace: pod spec must set securityContext.runAsUser to a non-zero value"
}

# Deny chutes namespace if any container runs as root (uses single helper for exception)
deny contains msg if {
	chutes_apply_pod_spec_rules
	input.request.namespace == "chutes"
	input.request.kind.kind == "Pod"
	helpers.is_pod_resource
	container := input.request.object.spec.containers[_]
	chutes_container_runs_root_denied(container, input.request.object.spec, false)
	msg := sprintf("Chutes namespace: container '%s' must not run as root (runAsUser: 0)", [container.name])
}

deny contains msg if {
	chutes_apply_pod_spec_rules
	input.request.namespace == "chutes"
	input.request.kind.kind == "Pod"
	helpers.is_pod_resource
	container := input.request.object.spec.initContainers[_]
	chutes_container_runs_root_denied(container, input.request.object.spec, true)
	msg := sprintf("Chutes namespace: init container '%s' must not run as root (runAsUser: 0)", [container.name])
}

deny contains msg if {
	chutes_apply_pod_spec_rules
	input.request.namespace == "chutes"
	input.request.kind.kind == "Pod"
	helpers.is_pod_resource
	container := input.request.object.spec.ephemeralContainers[_]
	chutes_container_runs_root_denied(container, input.request.object.spec, false)
	msg := sprintf("Chutes namespace: ephemeral container '%s' must not run as root (runAsUser: 0)", [container.name])
}

# Same for workload templates (Deployment, StatefulSet, DaemonSet, ReplicaSet, Job, CronJob)
deny contains msg if {
	chutes_apply_pod_spec_rules
	input.request.namespace == "chutes"
	input.request.kind.kind in ["Deployment", "StatefulSet", "DaemonSet", "ReplicaSet"]
	helpers.is_pod_resource
	input.request.object.spec.template.spec.securityContext.runAsUser == 0
	msg := "Chutes namespace: pods must not run as root (runAsUser: 0)"
}

deny contains msg if {
	chutes_apply_pod_spec_rules
	input.request.namespace == "chutes"
	input.request.kind.kind in ["Deployment", "StatefulSet", "DaemonSet", "ReplicaSet"]
	helpers.is_pod_resource
	not input.request.object.spec.template.spec.securityContext.runAsUser
	msg := "Chutes namespace: pod spec must set securityContext.runAsUser to a non-zero value"
}

deny contains msg if {
	chutes_apply_pod_spec_rules
	input.request.namespace == "chutes"
	input.request.kind.kind in ["Deployment", "StatefulSet", "DaemonSet", "ReplicaSet"]
	helpers.is_pod_resource
	container := input.request.object.spec.template.spec.containers[_]
	chutes_container_runs_root_denied(container, input.request.object.spec.template.spec, false)
	msg := sprintf("Chutes namespace: container '%s' must not run as root (runAsUser: 0)", [container.name])
}

deny contains msg if {
	chutes_apply_pod_spec_rules
	input.request.namespace == "chutes"
	input.request.kind.kind in ["Deployment", "StatefulSet", "DaemonSet", "ReplicaSet"]
	helpers.is_pod_resource
	container := input.request.object.spec.template.spec.initContainers[_]
	chutes_container_runs_root_denied(container, input.request.object.spec.template.spec, true)
	msg := sprintf("Chutes namespace: init container '%s' must not run as root (runAsUser: 0)", [container.name])
}

deny contains msg if {
	chutes_apply_pod_spec_rules
	input.request.namespace == "chutes"
	input.request.kind.kind == "Job"
	helpers.is_pod_resource
	input.request.object.spec.template.spec.securityContext.runAsUser == 0
	msg := "Chutes namespace: pods must not run as root (runAsUser: 0)"
}

deny contains msg if {
	chutes_apply_pod_spec_rules
	input.request.namespace == "chutes"
	input.request.kind.kind == "Job"
	helpers.is_pod_resource
	not input.request.object.spec.template.spec.securityContext.runAsUser
	msg := "Chutes namespace: pod spec must set securityContext.runAsUser to a non-zero value"
}

deny contains msg if {
	chutes_apply_pod_spec_rules
	input.request.namespace == "chutes"
	input.request.kind.kind == "Job"
	helpers.is_pod_resource
	container := input.request.object.spec.template.spec.containers[_]
	chutes_container_runs_root_denied(container, input.request.object.spec.template.spec, false)
	msg := sprintf("Chutes namespace: container '%s' must not run as root (runAsUser: 0)", [container.name])
}

deny contains msg if {
	chutes_apply_pod_spec_rules
	input.request.namespace == "chutes"
	input.request.kind.kind == "Job"
	helpers.is_pod_resource
	container := input.request.object.spec.template.spec.initContainers[_]
	chutes_container_runs_root_denied(container, input.request.object.spec.template.spec, true)
	msg := sprintf("Chutes namespace: init container '%s' must not run as root (runAsUser: 0)", [container.name])
}

deny contains msg if {
	chutes_apply_pod_spec_rules
	input.request.namespace == "chutes"
	input.request.kind.kind == "CronJob"
	helpers.is_pod_resource
	input.request.object.spec.jobTemplate.spec.template.spec.securityContext.runAsUser == 0
	msg := "Chutes namespace: pods must not run as root (runAsUser: 0)"
}

deny contains msg if {
	chutes_apply_pod_spec_rules
	input.request.namespace == "chutes"
	input.request.kind.kind == "CronJob"
	helpers.is_pod_resource
	not input.request.object.spec.jobTemplate.spec.template.spec.securityContext.runAsUser
	msg := "Chutes namespace: pod spec must set securityContext.runAsUser to a non-zero value"
}

deny contains msg if {
	chutes_apply_pod_spec_rules
	input.request.namespace == "chutes"
	input.request.kind.kind == "CronJob"
	helpers.is_pod_resource
	container := input.request.object.spec.jobTemplate.spec.template.spec.containers[_]
	chutes_container_runs_root_denied(container, input.request.object.spec.jobTemplate.spec.template.spec, false)
	msg := sprintf("Chutes namespace: container '%s' must not run as root (runAsUser: 0)", [container.name])
}

deny contains msg if {
	chutes_apply_pod_spec_rules
	input.request.namespace == "chutes"
	input.request.kind.kind == "CronJob"
	helpers.is_pod_resource
	container := input.request.object.spec.jobTemplate.spec.template.spec.initContainers[_]
	chutes_container_runs_root_denied(container, input.request.object.spec.jobTemplate.spec.template.spec, true)
	msg := sprintf("Chutes namespace: init container '%s' must not run as root (runAsUser: 0)", [container.name])
}

# =============================================================================
# CHUTES NAMESPACE: runAsNonRoot FOR NON-CHUTE WORKLOADS
# =============================================================================
# All pods in chutes must set runAsNonRoot: true except the chute workload: a Job
# (and the Pod that Job creates) from build_chute_job, which uses cache-init with runAsUser: 0.
# Only Job and Pod with label chutes/chute: "true" are treated as chute workloads;
# Deployment/StatefulSet/DaemonSet/ReplicaSet/CronJob with that label are not legitimate.

chutes_is_chute_workload if {
	input.request.namespace == "chutes"
	input.request.kind.kind == "Pod"
	input.request.object.metadata.labels["chutes/chute"] == "true"
}

chutes_is_chute_workload if {
	input.request.namespace == "chutes"
	input.request.kind.kind == "Job"
	input.request.object.spec.template.metadata.labels["chutes/chute"] == "true"
}

# Deny non-chute pods in chutes that do not set runAsNonRoot: true
deny contains msg if {
	chutes_apply_pod_spec_rules
	input.request.namespace == "chutes"
	input.request.kind.kind == "Pod"
	helpers.is_pod_resource
	not chutes_is_chute_workload
	not input.request.object.spec.securityContext.runAsNonRoot
	msg := "Chutes namespace: pod spec must set securityContext.runAsNonRoot: true (chute workloads excepted)"
}

deny contains msg if {
	chutes_apply_pod_spec_rules
	input.request.namespace == "chutes"
	input.request.kind.kind in ["Deployment", "StatefulSet", "DaemonSet", "ReplicaSet"]
	helpers.is_pod_resource
	not input.request.object.spec.template.spec.securityContext.runAsNonRoot
	msg := "Chutes namespace: pod spec must set securityContext.runAsNonRoot: true (chute workloads excepted)"
}

deny contains msg if {
	chutes_apply_pod_spec_rules
	input.request.namespace == "chutes"
	input.request.kind.kind == "Job"
	helpers.is_pod_resource
	not chutes_is_chute_workload
	not input.request.object.spec.template.spec.securityContext.runAsNonRoot
	msg := "Chutes namespace: pod spec must set securityContext.runAsNonRoot: true (chute workloads excepted)"
}

deny contains msg if {
	chutes_apply_pod_spec_rules
	input.request.namespace == "chutes"
	input.request.kind.kind == "CronJob"
	helpers.is_pod_resource
	not input.request.object.spec.jobTemplate.spec.template.spec.securityContext.runAsNonRoot
	msg := "Chutes namespace: pod spec must set securityContext.runAsNonRoot: true (chute workloads excepted)"
}

# =============================================================================
# CHUTES NAMESPACE: NO SERVICE ACCOUNT TOKEN
# =============================================================================
# Defense-in-depth: the mutating webhook sets automountServiceAccountToken: false
# before validation runs. These rules catch anything the mutator misses.
#
# Exception: the agent Deployment needs in-cluster API access. At the Pod level
# we verify the request originates from a system controller (replicaset-controller),
# not the miner, to prevent a miner from creating a bare pod that mimics the agent.

chutes_agent_sa_token_exempt if {
	input.request.kind.kind == "Pod"
	helpers.is_system_or_controller_user
	input.request.object.metadata.labels["app.kubernetes.io/name"] == "agent"
	has_agent_image(input.request.object.spec)
}

chutes_agent_sa_token_exempt if {
	input.request.kind.kind in ["Deployment", "StatefulSet", "DaemonSet", "ReplicaSet"]
	helpers.is_system_or_controller_user
	input.request.object.spec.template.metadata.labels["app.kubernetes.io/name"] == "agent"
	has_agent_image(input.request.object.spec.template.spec)
}

deny contains msg if {
	chutes_apply_pod_spec_rules
	input.request.namespace == "chutes"
	input.request.kind.kind == "Pod"
	helpers.is_pod_resource
	not chutes_agent_sa_token_exempt
	not input.request.object.spec.automountServiceAccountToken == false
	msg := "Chutes namespace: pod spec must set automountServiceAccountToken: false"
}

deny contains msg if {
	chutes_apply_pod_spec_rules
	input.request.namespace == "chutes"
	input.request.kind.kind in ["Deployment", "StatefulSet", "DaemonSet", "ReplicaSet"]
	helpers.is_pod_resource
	not chutes_agent_sa_token_exempt
	not input.request.object.spec.template.spec.automountServiceAccountToken == false
	msg := "Chutes namespace: pod spec must set automountServiceAccountToken: false"
}

deny contains msg if {
	chutes_apply_pod_spec_rules
	input.request.namespace == "chutes"
	input.request.kind.kind == "Job"
	helpers.is_pod_resource
	not input.request.object.spec.template.spec.automountServiceAccountToken == false
	msg := "Chutes namespace: pod spec must set automountServiceAccountToken: false"
}

deny contains msg if {
	chutes_apply_pod_spec_rules
	input.request.namespace == "chutes"
	input.request.kind.kind == "CronJob"
	helpers.is_pod_resource
	not input.request.object.spec.jobTemplate.spec.template.spec.automountServiceAccountToken == false
	msg := "Chutes namespace: pod spec must set automountServiceAccountToken: false"
}

# =============================================================================
# CHUTES NAMESPACE: IMAGE REGISTRY ALLOWLIST
# =============================================================================
# Only images from the validator registry (cosign-verified chute images) or the
# parachutes Docker Hub org (agent, cache-cleaner, etc.) are permitted. This is
# defense-in-depth on top of containerd cosign signature verification.

chutes_is_allowed_image(container) if {
	startswith(lower(container.image), concat("", [lower(data.config.validator_registry), "/"]))
}

chutes_is_allowed_image(container) if {
	startswith(lower(container.image), "parachutes/")
}

# Pod containers
deny contains msg if {
	chutes_apply_pod_spec_rules
	input.request.namespace == "chutes"
	input.request.kind.kind == "Pod"
	helpers.is_pod_resource
	not helpers.is_system_or_controller_user
	container := input.request.object.spec.containers[_]
	not chutes_is_allowed_image(container)
	msg := sprintf("Chutes namespace: container '%s' image '%s' is not from an allowed registry (validator registry or parachutes/)", [container.name, container.image])
}

deny contains msg if {
	chutes_apply_pod_spec_rules
	input.request.namespace == "chutes"
	input.request.kind.kind == "Pod"
	helpers.is_pod_resource
	not helpers.is_system_or_controller_user
	container := input.request.object.spec.initContainers[_]
	not chutes_is_allowed_image(container)
	msg := sprintf("Chutes namespace: init container '%s' image '%s' is not from an allowed registry", [container.name, container.image])
}

deny contains msg if {
	chutes_apply_pod_spec_rules
	input.request.namespace == "chutes"
	input.request.kind.kind == "Pod"
	helpers.is_pod_resource
	not helpers.is_system_or_controller_user
	container := input.request.object.spec.ephemeralContainers[_]
	not chutes_is_allowed_image(container)
	msg := sprintf("Chutes namespace: ephemeral container '%s' image '%s' is not from an allowed registry", [container.name, container.image])
}

# Workload controllers (Deployment, StatefulSet, DaemonSet, ReplicaSet, Job)
deny contains msg if {
	chutes_apply_pod_spec_rules
	input.request.namespace == "chutes"
	input.request.kind.kind in ["Deployment", "StatefulSet", "DaemonSet", "ReplicaSet", "Job"]
	helpers.is_pod_resource
	not helpers.is_system_or_controller_user
	container := input.request.object.spec.template.spec.containers[_]
	not chutes_is_allowed_image(container)
	msg := sprintf("Chutes namespace: container '%s' image '%s' is not from an allowed registry (validator registry or parachutes/)", [container.name, container.image])
}

deny contains msg if {
	chutes_apply_pod_spec_rules
	input.request.namespace == "chutes"
	input.request.kind.kind in ["Deployment", "StatefulSet", "DaemonSet", "ReplicaSet", "Job"]
	helpers.is_pod_resource
	not helpers.is_system_or_controller_user
	container := input.request.object.spec.template.spec.initContainers[_]
	not chutes_is_allowed_image(container)
	msg := sprintf("Chutes namespace: init container '%s' image '%s' is not from an allowed registry", [container.name, container.image])
}

# CronJob
deny contains msg if {
	chutes_apply_pod_spec_rules
	input.request.namespace == "chutes"
	input.request.kind.kind == "CronJob"
	helpers.is_pod_resource
	not helpers.is_system_or_controller_user
	container := input.request.object.spec.jobTemplate.spec.template.spec.containers[_]
	not chutes_is_allowed_image(container)
	msg := sprintf("Chutes namespace: container '%s' image '%s' is not from an allowed registry (validator registry or parachutes/)", [container.name, container.image])
}

deny contains msg if {
	chutes_apply_pod_spec_rules
	input.request.namespace == "chutes"
	input.request.kind.kind == "CronJob"
	helpers.is_pod_resource
	not helpers.is_system_or_controller_user
	container := input.request.object.spec.jobTemplate.spec.template.spec.initContainers[_]
	not chutes_is_allowed_image(container)
	msg := sprintf("Chutes namespace: init container '%s' image '%s' is not from an allowed registry", [container.name, container.image])
}

# =============================================================================
# CHUTES NAMESPACE: NO EPHEMERAL CONTAINERS
# =============================================================================
# Ephemeral containers are the only mutable part of a Pod spec (added via
# UPDATE/PATCH). They bypass all CREATE-only Pod rules and provide an
# interactive debugging shell inside a running pod. No legitimate use case
# exists in production — debugging is via the system manager API only.
# This rule fires on both CREATE and UPDATE to block kubectl debug entirely.

deny contains msg if {
	input.request.operation in ["CREATE", "UPDATE"]
	input.request.namespace == "chutes"
	input.request.kind.kind == "Pod"
	helpers.is_pod_resource
	input.request.object.spec.ephemeralContainers
	msg := "Chutes namespace: ephemeral containers are not permitted (no kubectl debug in production)"
}

# =============================================================================
# CHUTES NAMESPACE: NO envFrom
# =============================================================================
# envFrom bulk-injects all keys from a ConfigMap/Secret as env vars, bypassing
# the per-name allowlist in pods.rego. All env vars must use explicit env[]
# entries so each name is validated.

deny contains msg if {
	chutes_apply_pod_spec_rules
	input.request.namespace == "chutes"
	input.request.kind.kind == "Pod"
	helpers.is_pod_resource
	container := input.request.object.spec.containers[_]
	container.envFrom
	msg := sprintf("Chutes namespace: container '%s' must not use envFrom (use explicit env[] entries)", [container.name])
}

deny contains msg if {
	chutes_apply_pod_spec_rules
	input.request.namespace == "chutes"
	input.request.kind.kind == "Pod"
	helpers.is_pod_resource
	container := input.request.object.spec.initContainers[_]
	container.envFrom
	msg := sprintf("Chutes namespace: init container '%s' must not use envFrom", [container.name])
}

deny contains msg if {
	chutes_apply_pod_spec_rules
	input.request.namespace == "chutes"
	input.request.kind.kind == "Pod"
	helpers.is_pod_resource
	container := input.request.object.spec.ephemeralContainers[_]
	container.envFrom
	msg := sprintf("Chutes namespace: ephemeral container '%s' must not use envFrom", [container.name])
}

deny contains msg if {
	chutes_apply_pod_spec_rules
	input.request.namespace == "chutes"
	input.request.kind.kind in ["Deployment", "StatefulSet", "DaemonSet", "ReplicaSet"]
	helpers.is_pod_resource
	container := input.request.object.spec.template.spec.containers[_]
	container.envFrom
	msg := sprintf("Chutes namespace: container '%s' must not use envFrom (use explicit env[] entries)", [container.name])
}

deny contains msg if {
	chutes_apply_pod_spec_rules
	input.request.namespace == "chutes"
	input.request.kind.kind in ["Deployment", "StatefulSet", "DaemonSet", "ReplicaSet"]
	helpers.is_pod_resource
	container := input.request.object.spec.template.spec.initContainers[_]
	container.envFrom
	msg := sprintf("Chutes namespace: init container '%s' must not use envFrom", [container.name])
}

deny contains msg if {
	chutes_apply_pod_spec_rules
	input.request.namespace == "chutes"
	input.request.kind.kind == "Job"
	helpers.is_pod_resource
	container := input.request.object.spec.template.spec.containers[_]
	container.envFrom
	msg := sprintf("Chutes namespace: container '%s' must not use envFrom (use explicit env[] entries)", [container.name])
}

deny contains msg if {
	chutes_apply_pod_spec_rules
	input.request.namespace == "chutes"
	input.request.kind.kind == "Job"
	helpers.is_pod_resource
	container := input.request.object.spec.template.spec.initContainers[_]
	container.envFrom
	msg := sprintf("Chutes namespace: init container '%s' must not use envFrom", [container.name])
}

deny contains msg if {
	chutes_apply_pod_spec_rules
	input.request.namespace == "chutes"
	input.request.kind.kind == "CronJob"
	helpers.is_pod_resource
	container := input.request.object.spec.jobTemplate.spec.template.spec.containers[_]
	container.envFrom
	msg := sprintf("Chutes namespace: container '%s' must not use envFrom (use explicit env[] entries)", [container.name])
}

deny contains msg if {
	chutes_apply_pod_spec_rules
	input.request.namespace == "chutes"
	input.request.kind.kind == "CronJob"
	helpers.is_pod_resource
	container := input.request.object.spec.jobTemplate.spec.template.spec.initContainers[_]
	container.envFrom
	msg := sprintf("Chutes namespace: init container '%s' must not use envFrom", [container.name])
}

# =============================================================================
# CHUTES NAMESPACE: SECCOMP PROFILE ENFORCEMENT
# =============================================================================
# Containerd defaults to user-workload.json (set at VM build time). Any
# explicit seccompProfile override — even RuntimeDefault or Localhost —
# would weaken or replace it. Deny all seccompProfile specifications.

deny contains msg if {
	chutes_apply_pod_spec_rules
	input.request.namespace == "chutes"
	input.request.kind.kind == "Pod"
	helpers.is_pod_resource
	input.request.object.spec.securityContext.seccompProfile
	msg := "Chutes namespace: seccompProfile must not be specified (containerd default is enforced at build time)"
}

deny contains msg if {
	chutes_apply_pod_spec_rules
	input.request.namespace == "chutes"
	input.request.kind.kind == "Pod"
	helpers.is_pod_resource
	container := input.request.object.spec.containers[_]
	container.securityContext.seccompProfile
	msg := sprintf("Chutes namespace: container '%s' must not specify seccompProfile", [container.name])
}

deny contains msg if {
	chutes_apply_pod_spec_rules
	input.request.namespace == "chutes"
	input.request.kind.kind == "Pod"
	helpers.is_pod_resource
	container := input.request.object.spec.initContainers[_]
	container.securityContext.seccompProfile
	msg := sprintf("Chutes namespace: init container '%s' must not specify seccompProfile", [container.name])
}

deny contains msg if {
	chutes_apply_pod_spec_rules
	input.request.namespace == "chutes"
	input.request.kind.kind in ["Deployment", "StatefulSet", "DaemonSet", "ReplicaSet"]
	helpers.is_pod_resource
	input.request.object.spec.template.spec.securityContext.seccompProfile
	msg := "Chutes namespace: seccompProfile must not be specified (containerd default is enforced at build time)"
}

deny contains msg if {
	chutes_apply_pod_spec_rules
	input.request.namespace == "chutes"
	input.request.kind.kind in ["Deployment", "StatefulSet", "DaemonSet", "ReplicaSet"]
	helpers.is_pod_resource
	container := input.request.object.spec.template.spec.containers[_]
	container.securityContext.seccompProfile
	msg := sprintf("Chutes namespace: container '%s' must not specify seccompProfile", [container.name])
}

deny contains msg if {
	chutes_apply_pod_spec_rules
	input.request.namespace == "chutes"
	input.request.kind.kind == "Job"
	helpers.is_pod_resource
	input.request.object.spec.template.spec.securityContext.seccompProfile
	msg := "Chutes namespace: seccompProfile must not be specified (containerd default is enforced at build time)"
}

deny contains msg if {
	chutes_apply_pod_spec_rules
	input.request.namespace == "chutes"
	input.request.kind.kind == "Job"
	helpers.is_pod_resource
	container := input.request.object.spec.template.spec.containers[_]
	container.securityContext.seccompProfile
	msg := sprintf("Chutes namespace: container '%s' must not specify seccompProfile", [container.name])
}

deny contains msg if {
	chutes_apply_pod_spec_rules
	input.request.namespace == "chutes"
	input.request.kind.kind == "CronJob"
	helpers.is_pod_resource
	input.request.object.spec.jobTemplate.spec.template.spec.securityContext.seccompProfile
	msg := "Chutes namespace: seccompProfile must not be specified (containerd default is enforced at build time)"
}

# =============================================================================
# CHUTES NAMESPACE: VOLUME TYPE ALLOWLIST
# =============================================================================
# Prevent code injection via filesystem overlays (e.g. mounting a ConfigMap over
# a Python package directory to replace cosign-verified image code).
#
# Allowed types:
#   - hostPath  (validated separately by volumes.rego)
#   - emptyDir  (starts empty, safe)
#   - projected (auto-injected by k8s for SA token, kube-root-ca.crt, downwardAPI)
#
# Denied: configMap, secret, persistentVolumeClaim, nfs, csi, etc.
#
# Pod-level checks exempt system/controller users (e.g. kube-system:daemon-set-controller
# creating registry pods). We use userInfo.username — set by the API server after
# authentication — NOT ownerReferences, which are user-settable metadata and could be
# forged by a miner to bypass this policy.

chutes_is_allowed_volume_type(volume) if volume.hostPath
chutes_is_allowed_volume_type(volume) if volume.emptyDir != null
chutes_is_allowed_volume_type(volume) if volume.projected

deny contains msg if {
	chutes_apply_pod_spec_rules
	input.request.namespace == "chutes"
	input.request.kind.kind == "Pod"
	helpers.is_pod_resource
	not helpers.is_system_or_controller_user
	volume := input.request.object.spec.volumes[_]
	not chutes_is_allowed_volume_type(volume)
	msg := sprintf("Chutes namespace: volume '%s' uses a forbidden type (only hostPath, emptyDir, and projected allowed)", [volume.name])
}

deny contains msg if {
	chutes_apply_pod_spec_rules
	input.request.namespace == "chutes"
	input.request.kind.kind in ["Deployment", "StatefulSet", "DaemonSet", "ReplicaSet"]
	helpers.is_pod_resource
	volume := input.request.object.spec.template.spec.volumes[_]
	not chutes_is_allowed_volume_type(volume)
	msg := sprintf("Chutes namespace: volume '%s' uses a forbidden type (only hostPath, emptyDir, and projected allowed)", [volume.name])
}

deny contains msg if {
	chutes_apply_pod_spec_rules
	input.request.namespace == "chutes"
	input.request.kind.kind == "Job"
	helpers.is_pod_resource
	volume := input.request.object.spec.template.spec.volumes[_]
	not chutes_is_allowed_volume_type(volume)
	msg := sprintf("Chutes namespace: volume '%s' uses a forbidden type (only hostPath, emptyDir, and projected allowed)", [volume.name])
}

deny contains msg if {
	chutes_apply_pod_spec_rules
	input.request.namespace == "chutes"
	input.request.kind.kind == "CronJob"
	helpers.is_pod_resource
	volume := input.request.object.spec.jobTemplate.spec.template.spec.volumes[_]
	not chutes_is_allowed_volume_type(volume)
	msg := sprintf("Chutes namespace: volume '%s' uses a forbidden type (only hostPath, emptyDir, and projected allowed)", [volume.name])
}

# =============================================================================
# CHUTES NAMESPACE: COMMAND RESTRICTIONS
# =============================================================================
# In chutes namespace:
# - All containers (including init) must use image entrypoint only: no command override.
# - Exception: the main container named "chute" may set command but it must start
#   with ["chutes", "run"] (dynamic args after that are allowed).

# True when this container in chutes namespace should be denied (command override or invalid chute command)
chutes_deny_container(container) if {
	container.command
	container.name != "chute"
}

chutes_deny_container(container) if {
	container.command
	container.name == "chute"
	count(container.command) < 2
}

chutes_deny_container(container) if {
	container.command
	container.name == "chute"
	container.command[0] != "chutes"
}

chutes_deny_container(container) if {
	container.command
	container.name == "chute"
	container.command[1] != "run"
}

# Deny Pod in chutes namespace
deny contains msg if {
	chutes_apply_pod_spec_rules
	input.request.namespace == "chutes"
	input.request.kind.kind == "Pod"
	helpers.is_pod_resource
	container := input.request.object.spec.containers[_]
	chutes_deny_container(container)
	msg := chutes_deny_message(container)
}

deny contains msg if {
	chutes_apply_pod_spec_rules
	input.request.namespace == "chutes"
	input.request.kind.kind == "Pod"
	helpers.is_pod_resource
	container := input.request.object.spec.initContainers[_]
	chutes_deny_container(container)
	msg := sprintf("Chutes namespace: init container '%s' must not override command (use image entrypoint)", [container.name])
}

deny contains msg if {
	chutes_apply_pod_spec_rules
	input.request.namespace == "chutes"
	input.request.kind.kind == "Pod"
	helpers.is_pod_resource
	container := input.request.object.spec.ephemeralContainers[_]
	chutes_deny_container(container)
	msg := sprintf("Chutes namespace: ephemeral container '%s' must not override command (use image entrypoint)", [container.name])
}

# Deny Deployment/StatefulSet/DaemonSet/ReplicaSet in chutes namespace
deny contains msg if {
	chutes_apply_pod_spec_rules
	input.request.namespace == "chutes"
	input.request.kind.kind in ["Deployment", "StatefulSet", "DaemonSet", "ReplicaSet"]
	helpers.is_pod_resource
	container := input.request.object.spec.template.spec.containers[_]
	chutes_deny_container(container)
	msg := chutes_deny_message(container)
}

deny contains msg if {
	chutes_apply_pod_spec_rules
	input.request.namespace == "chutes"
	input.request.kind.kind in ["Deployment", "StatefulSet", "DaemonSet", "ReplicaSet"]
	helpers.is_pod_resource
	container := input.request.object.spec.template.spec.initContainers[_]
	chutes_deny_container(container)
	msg := sprintf("Chutes namespace: init container '%s' must not override command (use image entrypoint)", [container.name])
}

# Deny Job in chutes namespace
deny contains msg if {
	chutes_apply_pod_spec_rules
	input.request.namespace == "chutes"
	input.request.kind.kind == "Job"
	helpers.is_pod_resource
	container := input.request.object.spec.template.spec.containers[_]
	chutes_deny_container(container)
	msg := chutes_deny_message(container)
}

deny contains msg if {
	chutes_apply_pod_spec_rules
	input.request.namespace == "chutes"
	input.request.kind.kind == "Job"
	helpers.is_pod_resource
	container := input.request.object.spec.template.spec.initContainers[_]
	chutes_deny_container(container)
	msg := sprintf("Chutes namespace: init container '%s' must not override command (use image entrypoint)", [container.name])
}

# Deny CronJob in chutes namespace
deny contains msg if {
	chutes_apply_pod_spec_rules
	input.request.namespace == "chutes"
	input.request.kind.kind == "CronJob"
	helpers.is_pod_resource
	container := input.request.object.spec.jobTemplate.spec.template.spec.containers[_]
	chutes_deny_container(container)
	msg := chutes_deny_message(container)
}

deny contains msg if {
	chutes_apply_pod_spec_rules
	input.request.namespace == "chutes"
	input.request.kind.kind == "CronJob"
	helpers.is_pod_resource
	container := input.request.object.spec.jobTemplate.spec.template.spec.initContainers[_]
	chutes_deny_container(container)
	msg := sprintf("Chutes namespace: init container '%s' must not override command (use image entrypoint)", [container.name])
}

# Message for main containers: chute must be "chutes run", others must not override
chutes_deny_message(container) := msg if {
	container.name == "chute"
	msg := sprintf("Chutes namespace: container '%s' command must start with ['chutes', 'run']", [container.name])
}

chutes_deny_message(container) := msg if {
	container.name != "chute"
	msg := sprintf("Chutes namespace: container '%s' must not override command (use image entrypoint)", [container.name])
}

# =============================================================================
# CHUTES NAMESPACE: CONFIGMAP MUTATIONS RESTRICTED TO SYSTEM CONTROLLERS
# =============================================================================
# The miner has RBAC for ConfigMaps (backwards compat with non-TEE deploys),
# but OPA denies all mutations from non-system users. This prevents:
#   - Modifying kube-root-ca.crt (CA injection for MITM)
#   - Modifying registry-nginx-config (registry traffic manipulation)
#   - Creating ConfigMaps for code injection (volume mount separately blocked)
#
# Only system/controller users (identified by userInfo.username, set by the
# API server after authentication) may create, update, or delete ConfigMaps.

deny contains msg if {
	input.request.namespace == "chutes"
	input.request.kind.kind == "ConfigMap"
	input.request.operation in ["CREATE", "UPDATE", "DELETE"]
	not helpers.is_system_or_controller_user
	msg := sprintf("Chutes namespace: ConfigMap operations restricted to system controllers (user '%s' denied)", [input.request.userInfo.username])
}

# =============================================================================
# CHUTES NAMESPACE: NO LIFECYCLE HOOKS
# =============================================================================
# The command rules exist so only the signed image's code runs. A lifecycle hook voids that: it
# takes arbitrary argv, is never matched by chutes_deny_container (which reads only
# container.command), and runs as a separate process even when the entrypoint aborts. Worst on
# cache-init, whose root carve-out assumes only its own entrypoint runs.
# Flat deny — no legitimate chute spec sets one. Probe exec is constrained separately below.

chutes_all_containers(spec) := array.concat(
	array.concat(
		object.get(spec, "containers", []),
		object.get(spec, "initContainers", []),
	),
	object.get(spec, "ephemeralContainers", []),
)

chutes_lifecycle_msg(container) := sprintf(
	"Chutes namespace: container '%s' must not set lifecycle hooks (postStart/preStop); only the image entrypoint may execute",
	[container.name],
)

deny contains msg if {
	chutes_apply_pod_spec_rules
	input.request.namespace == "chutes"
	input.request.kind.kind == "Pod"
	helpers.is_pod_resource
	some container in chutes_all_containers(input.request.object.spec)
	container.lifecycle
	msg := chutes_lifecycle_msg(container)
}

deny contains msg if {
	chutes_apply_pod_spec_rules
	input.request.namespace == "chutes"
	input.request.kind.kind in ["Deployment", "StatefulSet", "DaemonSet", "ReplicaSet", "Job"]
	helpers.is_pod_resource
	some container in chutes_all_containers(input.request.object.spec.template.spec)
	container.lifecycle
	msg := chutes_lifecycle_msg(container)
}

deny contains msg if {
	chutes_apply_pod_spec_rules
	input.request.namespace == "chutes"
	input.request.kind.kind == "CronJob"
	helpers.is_pod_resource
	some container in chutes_all_containers(input.request.object.spec.jobTemplate.spec.template.spec)
	container.lifecycle
	msg := chutes_lifecycle_msg(container)
}

# =============================================================================
# CHUTES NAMESPACE: PROBE EXEC MUST BE A PLAIN LOCALHOST CURL
# =============================================================================
# Same primitive as a lifecycle hook, but it cannot be blocked: the chute's middleware
# authenticates every request except from 127.0.0.1, so kubelet's httpGet probe (which dials the pod
# IP) is rejected and the probe must run inside the pod.
#
# So pin the shape. Port and path stay free; everything else is fixed. The regex is fully anchored
# because the command is a shell string — unanchored, `curl -f http://127.0.0.1:8000/x; <anything>`
# passes. Flags that read/write files or redirect the request (-o, -T, -K, -d) are excluded.
# httpGet/tcpSocket probes are untouched: kubelet runs those, no code in the container.

chutes_probe_command_allowed(command) if {
	count(command) == 3
	command[0] == "/bin/sh"
	command[1] == "-c"
	regex.match(`^curl( -[fsS]{1,3}| --fail| --silent| --show-error| -m [0-9]{1,3}| --max-time [0-9]{1,3})* http://127\.0\.0\.1:[0-9]{1,5}(/[A-Za-z0-9._~/-]*)?( \|\| exit [0-9]{1,3})?$`, command[2])
}

# The exec probes a container declares that are not a plain localhost curl.
chutes_bad_probe_exec(container) if {
	some probe_name in ["readinessProbe", "livenessProbe", "startupProbe"]
	probe := object.get(container, probe_name, {})
	exec_action := object.get(probe, "exec", {})
	exec_action != {}
	not chutes_probe_command_allowed(object.get(exec_action, "command", []))
}

chutes_probe_msg(container) := sprintf(
	"Chutes namespace: container '%s' probe exec must be a plain localhost curl (/bin/sh -c 'curl -f http://127.0.0.1:<port>/<path> || exit 1')",
	[container.name],
)

deny contains msg if {
	chutes_apply_pod_spec_rules
	input.request.namespace == "chutes"
	input.request.kind.kind == "Pod"
	helpers.is_pod_resource
	some container in chutes_all_containers(input.request.object.spec)
	chutes_bad_probe_exec(container)
	msg := chutes_probe_msg(container)
}

deny contains msg if {
	chutes_apply_pod_spec_rules
	input.request.namespace == "chutes"
	input.request.kind.kind in ["Deployment", "StatefulSet", "DaemonSet", "ReplicaSet", "Job"]
	helpers.is_pod_resource
	some container in chutes_all_containers(input.request.object.spec.template.spec)
	chutes_bad_probe_exec(container)
	msg := chutes_probe_msg(container)
}

deny contains msg if {
	chutes_apply_pod_spec_rules
	input.request.namespace == "chutes"
	input.request.kind.kind == "CronJob"
	helpers.is_pod_resource
	some container in chutes_all_containers(input.request.object.spec.jobTemplate.spec.template.spec)
	chutes_bad_probe_exec(container)
	msg := chutes_probe_msg(container)
}

# =============================================================================
# CHUTES NAMESPACE: NO PROJECTED VOLUMES ON CHUTE WORKLOADS
# =============================================================================
# `projected` is a container for exactly the source types the volume allowlist denies: secret,
# configMap, downwardAPI and serviceAccountToken. The last one is the reason this matters —
# automountServiceAccountToken: false only suppresses the auto-injected volume, so an explicitly
# declared serviceAccountToken source still gets a live kube API token. Untrusted chute code must
# not reach the API at all; job management is the miner's own granted right, exercised with the
# miner's credentials, not something a chute pod inherits.
#
# Non-chute pods in the namespace keep projected: failed-chute-cleanup (deployed into the guest by
# sek8s from the chutes-miner-gpu chart) legitimately needs a serviceAccountToken source, and is not
# labelled chutes/chute=true. Chute workloads use only hostPath and emptyDir.

chutes_projected_msg(name) := sprintf(
	"Chutes namespace: chute workload may not use a projected volume ('%s'); projected sources include serviceAccountToken, which bypasses automountServiceAccountToken: false",
	[name],
)

deny contains msg if {
	chutes_apply_pod_spec_rules
	input.request.namespace == "chutes"
	helpers.is_pod_resource
	not helpers.is_system_or_controller_user
	chutes_is_chute_workload
	input.request.kind.kind == "Pod"
	some volume in input.request.object.spec.volumes
	volume.projected
	msg := chutes_projected_msg(volume.name)
}

deny contains msg if {
	chutes_apply_pod_spec_rules
	input.request.namespace == "chutes"
	helpers.is_pod_resource
	not helpers.is_system_or_controller_user
	chutes_is_chute_workload
	input.request.kind.kind == "Job"
	some volume in input.request.object.spec.template.spec.volumes
	volume.projected
	msg := chutes_projected_msg(volume.name)
}

# =============================================================================
# CHUTES NAMESPACE: CHUTE WORKLOADS MAY NOT CHOOSE A SERVICE ACCOUNT
# =============================================================================
# Nothing restricted spec.serviceAccountName, so a chute could name any SA in the namespace. The
# `agent` SA holds secrets get/list/watch in chutes, and 03-k3s-miner-credentials.sh reconciles
# miner-credentials -- which contains the miner seed -- into that namespace. Combined with a
# projected serviceAccountToken source (denied above) that was a direct read of the seed over the
# kube API. Denying both independently means neither step alone suffices.
#
# Real chute specs set no serviceAccountName, so they run as `default`, which has no RoleBinding.

chutes_sa_msg(name) := sprintf(
	"Chutes namespace: chute workload may not select serviceAccountName '%s'; chutes run as the unprivileged default account",
	[name],
)

chutes_bad_service_account(spec) := name if {
	name := object.get(spec, "serviceAccountName", "default")
	name != "default"
}

deny contains msg if {
	chutes_apply_pod_spec_rules
	input.request.namespace == "chutes"
	helpers.is_pod_resource
	not helpers.is_system_or_controller_user
	chutes_is_chute_workload
	input.request.kind.kind == "Pod"
	msg := chutes_sa_msg(chutes_bad_service_account(input.request.object.spec))
}

deny contains msg if {
	chutes_apply_pod_spec_rules
	input.request.namespace == "chutes"
	helpers.is_pod_resource
	not helpers.is_system_or_controller_user
	chutes_is_chute_workload
	input.request.kind.kind == "Job"
	msg := chutes_sa_msg(chutes_bad_service_account(input.request.object.spec.template.spec))
}

# =============================================================================
# A CHUTE WORKLOAD MUST BE CAPTURABLE BY THE LOG SHIPPER
# =============================================================================
# The shipper runs inside the attested guest precisely so the untrusted miner cannot
# turn it off. It could, though: the miner authors the pod spec, and the shipper finds
# a pod by its `chutes/config-id` label and reads only the container named "chute".
# Omit the label and the pod is invisible; name the container something else and its
# log directory is never opened. Neither cost the miner anything, because nothing else
# in the guest consulted either value -- so the two assumptions the shipper was built
# on are enforced here rather than merely documented.
#
# The label must be on the POD, which is what the shipper reads back off CRI: on the
# pod itself, or on a Job's spec.template.metadata.

chutes_workload_containers := object.get(input.request.object.spec, "containers", []) if {
	input.request.kind.kind == "Pod"
}

chutes_workload_containers := object.get(input.request.object.spec.template.spec, "containers", []) if {
	input.request.kind.kind == "Job"
}

chutes_workload_pod_labels := object.get(input.request.object.metadata, "labels", {}) if {
	input.request.kind.kind == "Pod"
}

chutes_workload_pod_labels := object.get(input.request.object.spec.template.metadata, "labels", {}) if {
	input.request.kind.kind == "Job"
}

chutes_has_main_container if {
	container := chutes_workload_containers[_]
	container.name == "chute"
}

# Non-empty, because the shipper skips a pod whose config_id is falsy -- an empty
# label hides it just as completely as a missing one.
chutes_has_config_id if {
	object.get(chutes_workload_pod_labels, "chutes/config-id", "") != ""
}

deny contains msg if {
	chutes_apply_pod_spec_rules
	input.request.namespace == "chutes"
	helpers.is_pod_resource
	chutes_is_chute_workload
	not chutes_has_main_container
	msg := "Chutes namespace: a chute workload must have a container named 'chute' (the log shipper captures only that container)"
}

deny contains msg if {
	chutes_apply_pod_spec_rules
	input.request.namespace == "chutes"
	helpers.is_pod_resource
	chutes_is_chute_workload
	not chutes_has_config_id
	msg := "Chutes namespace: a chute workload must carry a non-empty 'chutes/config-id' label (the log shipper discovers pods by it)"
}

# =============================================================================
# A CHUTE WORKLOAD MAY NOT SUPPLY CONTAINER ARGS
# =============================================================================
# `command` is tightly restricted above, but Kubernetes gives the pod author two ways
# to shape argv: when `command` is omitted, `args` replaces the image's CMD and is
# passed to its ENTRYPOINT. Restricting one and not the other enforced half of the
# entrypoint guarantee.
#
# Denied outright rather than pattern-matched, because nothing legitimate sets it: the
# real spec builder gives neither cache-init nor chute a `command` or an `args` — both
# run their image entrypoint and are configured entirely through env. A chute that
# genuinely needs trailing arguments still has the sanctioned route, `command` starting
# with ["chutes", "run"].
#
# Covers init and ephemeral containers too: that is where the design intent is
# strictest ("image entrypoint only") and where an arbitrary argv would be least visible.

chutes_workload_all_containers contains container if {
	some container in chutes_workload_containers
}

chutes_workload_all_containers contains container if {
	input.request.kind.kind == "Pod"
	some container in object.get(input.request.object.spec, "initContainers", [])
}

chutes_workload_all_containers contains container if {
	input.request.kind.kind == "Pod"
	some container in object.get(input.request.object.spec, "ephemeralContainers", [])
}

chutes_workload_all_containers contains container if {
	input.request.kind.kind == "Job"
	some container in object.get(input.request.object.spec.template.spec, "initContainers", [])
}

deny contains msg if {
	chutes_apply_pod_spec_rules
	input.request.namespace == "chutes"
	helpers.is_pod_resource
	chutes_is_chute_workload
	some container in chutes_workload_all_containers
	container.args
	msg := sprintf(
		"Chutes namespace: container '%s' must not set args (use the image entrypoint; a chute passes trailing arguments via command)",
		[container.name],
	)
}
