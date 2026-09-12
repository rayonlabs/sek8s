package kubernetes.admission

import future.keywords.contains
import future.keywords.if
import future.keywords.in

import data.helpers

# =============================================================================
# VOLUME MOUNT RESTRICTIONS
# =============================================================================

deny contains msg if {
	input.request.operation == "CREATE"
	helpers.is_pod_resource
	not helpers.is_system_or_controller_user

	# Check Pod directly
	input.request.kind.kind == "Pod"
	volume := input.request.object.spec.volumes[_]
	volume.hostPath
	not is_allowed_hostpath(volume.hostPath.path, input.request.object)
	msg := sprintf("hostPath volume '%s' not allowed.", [volume.hostPath.path])
}

deny contains msg if {
	input.request.operation in ["CREATE", "UPDATE"]
	helpers.is_pod_resource
	not helpers.is_system_or_controller_user
	not helpers.is_rollout_restart

	# Check Deployment/StatefulSet/DaemonSet templates
	input.request.kind.kind in ["Deployment", "StatefulSet", "DaemonSet", "ReplicaSet"]
	volume := input.request.object.spec.template.spec.volumes[_]
	volume.hostPath
	not is_allowed_hostpath(volume.hostPath.path, input.request.object)
	msg := sprintf("hostPath volume '%s' not allowed.", [volume.hostPath.path])
}

deny contains msg if {
	input.request.operation in ["CREATE", "UPDATE"]
	helpers.is_pod_resource
	not helpers.is_system_or_controller_user
	not helpers.is_rollout_restart

	# Check Job templates
	input.request.kind.kind == "Job"
	volume := input.request.object.spec.template.spec.volumes[_]
	volume.hostPath
	not is_allowed_hostpath(volume.hostPath.path, input.request.object)
	msg := sprintf("Job hostPath volume '%s' not allowed. Use emptyDir for temporary storage.", [volume.hostPath.path])
}

deny contains msg if {
	input.request.operation in ["CREATE", "UPDATE"]
	helpers.is_pod_resource
	not helpers.is_system_or_controller_user
	not helpers.is_rollout_restart

	# Check CronJob templates
	input.request.kind.kind == "CronJob"
	volume := input.request.object.spec.jobTemplate.spec.template.spec.volumes[_]
	volume.hostPath
	not is_allowed_hostpath(volume.hostPath.path, input.request.object)
	msg := sprintf("CronJob hostPath volume '%s' not allowed.", [volume.hostPath.path])
}

# =============================================================================
# HOSTPATH ALLOWLIST HELPERS
# =============================================================================

# Reject any path containing traversal sequences. Kubelet normalizes paths
# before the admission request reaches OPA, so this should never fire in
# practice — it's defense-in-depth against hypothetical bypass.
is_path_traversal(path) if contains(path, "..")

# Cache path: /var/snap/cache exactly or /var/snap/cache/... (not /var/snap/cache-evil)
is_cache_hostpath(path) if {
	not is_path_traversal(path)
	path == "/var/snap/cache"
}

is_cache_hostpath(path) if {
	not is_path_traversal(path)
	startswith(path, "/var/snap/cache/")
}

# Image from the validator registry (cosign-verified chute images).
# data.config.validator_registry is set at deploy time via config.json
# (e.g. "myvalidator.localregistry.chutes.ai:30500").
# lower() is required because k8s/containerd normalise registry hostnames
# to lowercase while the Ansible config may carry a mixed-case SS58 address.
# Require a "/" delimiter after the registry to prevent prefix collisions
# (e.g. "evil.localregistry.chutes.ai.evil.com/...").
has_validator_registry_image(pod_spec) if {
	container := pod_spec.containers[_]
	startswith(lower(container.image), concat("", [lower(data.config.validator_registry), "/"]))
}

# Image from parachutes/chutes-agent (cosign-verified via parachutes org key).
# Require [:@] delimiter after the image name to prevent prefix collisions
# (e.g. "parachutes/chutes-agent-evil" must not match).
has_agent_image(pod_spec) if {
	container := pod_spec.containers[_]
	regex.match("^parachutes/chutes-agent[:@]", lower(container.image))
}

# =============================================================================
# CACHE HOSTPATH: chute label + validator registry image + chutes namespace
# =============================================================================

# Pod: chute label + validator registry image
is_allowed_hostpath(path, obj) if {
	is_cache_hostpath(path)
	input.request.namespace == "chutes"
	obj.metadata.labels["chutes/chute"] == "true"
	has_validator_registry_image(obj.spec)
}

# Job/Deployment/etc: chute label on template + validator registry image
is_allowed_hostpath(path, obj) if {
	is_cache_hostpath(path)
	input.request.namespace == "chutes"
	obj.spec.template.metadata.labels["chutes/chute"] == "true"
	has_validator_registry_image(obj.spec.template.spec)
}

# =============================================================================
# AGENT HOSTPATH: agent label + parachutes/chutes-agent image + chutes namespace
# =============================================================================

# Pod
is_allowed_hostpath(path, obj) if {
	path == "/var/lib/chutes/agent"
	input.request.namespace == "chutes"
	obj.metadata.labels["app.kubernetes.io/name"] == "agent"
	has_agent_image(obj.spec)
}

# Deployment/etc: agent label on template
is_allowed_hostpath(path, obj) if {
	path == "/var/lib/chutes/agent"
	input.request.namespace == "chutes"
	obj.spec.template.metadata.labels["app.kubernetes.io/name"] == "agent"
	has_agent_image(obj.spec.template.spec)
}

# =============================================================================
# FULL CACHE MOUNT: only the cache-cleaner init container may mount the tree root
# =============================================================================
# /var/snap/cache is a pod-level volume so cache-init can scan the tree for eviction. Volumes are
# pod-scoped, so without this any other container in the pod could mount it too — the tree is 2775
# owned 1000:1000 and chute containers run as 1000, i.e. every chute's weights. Anchored on
# chutes_is_cache_cleaner_init (name + signed image), which the miner cannot forge.

is_full_cache_volume(volume) if {
	volume.hostPath.path == "/var/snap/cache"
}

full_cache_volume_names(spec) := {volume.name |
	some volume in object.get(spec, "volumes", [])
	is_full_cache_volume(volume)
}

mounts_full_cache(container, spec) if {
	some mount in object.get(container, "volumeMounts", [])
	mount.name in full_cache_volume_names(spec)
}

# Every container that must NOT reach the whole cache tree: all main and ephemeral containers,
# plus any init container that is not the cache cleaner.
cache_restricted_containers(spec) := array.concat(
	array.concat(
		object.get(spec, "containers", []),
		object.get(spec, "ephemeralContainers", []),
	),
	[container |
		some container in object.get(spec, "initContainers", [])
		not chutes_is_cache_cleaner_init(container)
	],
)

full_cache_mount_msg(container) := sprintf(
	"Container '%s' may not mount the shared cache root '/var/snap/cache'; only the cache-cleaner init container may. Use the per-chute cache volume.",
	[container.name],
)

deny contains msg if {
	input.request.operation in ["CREATE", "UPDATE"]
	helpers.is_pod_resource
	not helpers.is_system_or_controller_user
	not helpers.is_rollout_restart

	input.request.kind.kind == "Pod"
	spec := input.request.object.spec
	some container in cache_restricted_containers(spec)
	mounts_full_cache(container, spec)
	msg := full_cache_mount_msg(container)
}

deny contains msg if {
	input.request.operation in ["CREATE", "UPDATE"]
	helpers.is_pod_resource
	not helpers.is_system_or_controller_user
	not helpers.is_rollout_restart

	input.request.kind.kind in ["Deployment", "StatefulSet", "DaemonSet", "ReplicaSet", "Job"]
	spec := input.request.object.spec.template.spec
	some container in cache_restricted_containers(spec)
	mounts_full_cache(container, spec)
	msg := full_cache_mount_msg(container)
}

deny contains msg if {
	input.request.operation in ["CREATE", "UPDATE"]
	helpers.is_pod_resource
	not helpers.is_system_or_controller_user
	not helpers.is_rollout_restart

	input.request.kind.kind == "CronJob"
	spec := input.request.object.spec.jobTemplate.spec.template.spec
	some container in cache_restricted_containers(spec)
	mounts_full_cache(container, spec)
	msg := full_cache_mount_msg(container)
}

# =============================================================================
# PER-CHUTE CACHE MOUNT MUST MATCH THE LAUNCH TOKEN'S chute_id
# =============================================================================
# Stops the per-chute path being pointed at a co-resident chute's directory.
#
# The signature is not checked here: aegis verifies it at runtime (set_secure_fs, pid 1), so a
# forged token yields a mount with nothing able to read it. That holds only while nothing executes
# outside the signed entrypoint -- lifecycle hooks, probe exec, kubectl exec and command overrides
# are all denied. If any is relaxed, verify here with io.jwt.verify_es256.
# exp is unchecked: a controller re-creating a pod from an unchanged spec must not be denied.

# Read from the container named "chute" so a second token elsewhere cannot be selected instead.
chutes_declared_chute_id(spec) := chute_id if {
	some container in object.get(spec, "containers", [])
	container.name == "chute"
	some env_var in object.get(container, "env", [])
	env_var.name == "CHUTES_LAUNCH_JWT"
	[_, payload, _] := io.jwt.decode(object.get(env_var, "value", ""))
	chute_id := payload.chute_id
}

# Cache-tree mounts, excluding the root itself (covered above).
chutes_per_chute_cache_paths(spec) := {path |
	some volume in object.get(spec, "volumes", [])
	path := object.get(object.get(volume, "hostPath", {}), "path", "")
	startswith(path, "/var/snap/cache/")
}

# Either a different chute, or no readable token (fail closed).
chutes_cache_path_unverified(spec) := path if {
	some path in chutes_per_chute_cache_paths(spec)
	path != sprintf("/var/snap/cache/%s", [chutes_declared_chute_id(spec)])
}

chutes_cache_path_unverified(spec) := path if {
	some path in chutes_per_chute_cache_paths(spec)
	not chutes_declared_chute_id(spec)
}

chutes_cache_binding_msg(path) := sprintf(
	"Chutes namespace: cache hostPath '%s' does not match the chute_id in this pod's launch token",
	[path],
)

deny contains msg if {
	input.request.operation in ["CREATE", "UPDATE"]
	helpers.is_pod_resource
	not helpers.is_system_or_controller_user
	not helpers.is_rollout_restart
	input.request.namespace == "chutes"

	input.request.kind.kind == "Pod"
	msg := chutes_cache_binding_msg(chutes_cache_path_unverified(input.request.object.spec))
}

deny contains msg if {
	input.request.operation in ["CREATE", "UPDATE"]
	helpers.is_pod_resource
	not helpers.is_system_or_controller_user
	not helpers.is_rollout_restart
	input.request.namespace == "chutes"

	input.request.kind.kind in ["Deployment", "StatefulSet", "DaemonSet", "ReplicaSet", "Job"]
	msg := chutes_cache_binding_msg(chutes_cache_path_unverified(input.request.object.spec.template.spec))
}
