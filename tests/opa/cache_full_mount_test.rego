# Only the cache-cleaner init container may mount the cache tree root. Volumes are pod-scoped, so
# the volume cache-init needs for eviction is otherwise mountable by the chute container too.
package kubernetes.admission

import future.keywords.if
import future.keywords.in

full_cache_msg(name) := sprintf(
	"Container '%s' may not mount the shared cache root '/var/snap/cache'; only the cache-cleaner init container may. Use the per-chute cache volume.",
	[name],
)

# The real chute pod shape: per-chute cache volume plus the full-tree raw-cache volume that the
# cache-cleaner init container legitimately needs.
chute_pod(containers, init_containers) := {
	"operation": "CREATE",
	"namespace": "chutes",
	"kind": {"kind": "Pod"},
	"object": {
		"metadata": {"labels": {"chutes/chute": "true"}},
		"spec": {
			"securityContext": {"runAsUser": 1000},
			"containers": containers,
			"initContainers": init_containers,
			"volumes": [
				{"name": "cache", "hostPath": {"path": "/var/snap/cache/chute-abc"}},
				{"name": "raw-cache", "hostPath": {"path": "/var/snap/cache"}},
			],
		},
	},
}

cleaner := {
	"name": "cache-init",
	"image": "parachutes/cache-cleaner:release-next-latest",
	"volumeMounts": [{"name": "raw-cache", "mountPath": "/cache"}],
}

chute_own_cache := {
	"name": "chute",
	"image": "registry.chutes.ai/org/chute:latest",
	"volumeMounts": [{"name": "cache", "mountPath": "/cache"}],
}

chute_full_cache := {
	"name": "chute",
	"image": "registry.chutes.ai/org/chute:latest",
	"volumeMounts": [{"name": "raw-cache", "mountPath": "/loot"}],
}

# --- the regression: a chute container reaching the whole tree -----------------

test_deny_chute_container_mounting_full_cache if {
	req := chute_pod([chute_full_cache], [cleaner])
	deny[full_cache_msg("chute")] with input as {"request": req}
}

test_deny_chute_container_mounting_full_cache_without_any_init if {
	req := chute_pod([chute_full_cache], [])
	deny[full_cache_msg("chute")] with input as {"request": req}
}

# --- the legitimate shape must still pass -------------------------------------

test_allow_cache_cleaner_init_mounting_full_cache if {
	req := chute_pod([chute_own_cache], [cleaner])
	not deny[full_cache_msg("cache-init")] with input as {"request": req}
	not deny[full_cache_msg("chute")] with input as {"request": req}
}

test_allow_chute_container_mounting_its_own_cache if {
	req := chute_pod([chute_own_cache], [cleaner])
	not deny[full_cache_msg("chute")] with input as {"request": req}
}

# --- the cleaner exception must not be impersonable ---------------------------

test_deny_init_container_with_cleaner_name_but_other_image if {
	impostor := {
		"name": "cache-init",
		"image": "registry.chutes.ai/org/chute:latest",
		"volumeMounts": [{"name": "raw-cache", "mountPath": "/loot"}],
	}
	req := chute_pod([chute_own_cache], [impostor])
	deny[full_cache_msg("cache-init")] with input as {"request": req}
}

test_deny_init_container_with_cleaner_image_prefix_collision if {
	impostor := {
		"name": "cache-init",
		"image": "parachutes/cache-cleaner-evil:latest",
		"volumeMounts": [{"name": "raw-cache", "mountPath": "/loot"}],
	}
	req := chute_pod([chute_own_cache], [impostor])
	deny[full_cache_msg("cache-init")] with input as {"request": req}
}

test_deny_other_init_container_mounting_full_cache if {
	other := {
		"name": "setup",
		"image": "parachutes/cache-cleaner:latest",
		"volumeMounts": [{"name": "raw-cache", "mountPath": "/loot"}],
	}
	req := chute_pod([chute_own_cache], [other])
	deny[full_cache_msg("setup")] with input as {"request": req}
}

# --- other container lists and resource shapes --------------------------------

test_deny_ephemeral_container_mounting_full_cache if {
	req := {
		"operation": "UPDATE",
		"namespace": "chutes",
		"kind": {"kind": "Pod"},
		"object": {
			"metadata": {"labels": {"chutes/chute": "true"}},
			"spec": {
				"securityContext": {"runAsUser": 1000},
				"containers": [chute_own_cache],
				"ephemeralContainers": [{
					"name": "debug",
					"image": "registry.chutes.ai/org/chute:latest",
					"volumeMounts": [{"name": "raw-cache", "mountPath": "/loot"}],
				}],
				"volumes": [{"name": "raw-cache", "hostPath": {"path": "/var/snap/cache"}}],
			},
		},
	}
	deny[full_cache_msg("debug")] with input as {"request": req}
}

test_deny_job_template_container_mounting_full_cache if {
	req := {
		"operation": "CREATE",
		"namespace": "chutes",
		"kind": {"kind": "Job"},
		"object": {"spec": {"template": {
			"metadata": {"labels": {"chutes/chute": "true"}},
			"spec": {
				"securityContext": {"runAsUser": 1000},
				"containers": [chute_full_cache],
				"volumes": [{"name": "raw-cache", "hostPath": {"path": "/var/snap/cache"}}],
			},
		}}},
	}
	deny[full_cache_msg("chute")] with input as {"request": req}
}

# --- the rule keys on the volume's path, not its name -------------------------

test_deny_full_cache_under_a_renamed_volume if {
	req := {
		"operation": "CREATE",
		"namespace": "chutes",
		"kind": {"kind": "Pod"},
		"object": {
			"metadata": {"labels": {"chutes/chute": "true"}},
			"spec": {
				"securityContext": {"runAsUser": 1000},
				"containers": [{
					"name": "chute",
					"image": "registry.chutes.ai/org/chute:latest",
					"volumeMounts": [{"name": "innocuous", "mountPath": "/loot"}],
				}],
				"volumes": [{"name": "innocuous", "hostPath": {"path": "/var/snap/cache"}}],
			},
		},
	}
	deny[full_cache_msg("chute")] with input as {"request": req}
}
