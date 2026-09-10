# OPA tests: a chute workload may not supply container args.
#
# `command` was already restricted, but Kubernetes lets the pod author shape argv either
# way — with `command` omitted, `args` replaces the image CMD and reaches its ENTRYPOINT.
# Run locally: make test-opa-policies
package kubernetes.admission

import future.keywords.if
import future.keywords.in

cargs_labels := {"chutes/chute": "true", "chutes/config-id": "cfg-1"}

cargs_chute := {"name": "chute", "image": "parachutes/chute:1"}

cargs_pod(spec_extra) := {
	"operation": "CREATE",
	"namespace": "chutes",
	"kind": {"kind": "Pod"},
	"object": {
		"metadata": {"labels": cargs_labels},
		"spec": object.union({"containers": [cargs_chute]}, spec_extra),
	},
	"userInfo": {"username": "miner"},
}

cargs_job(spec_extra) := {
	"operation": "CREATE",
	"namespace": "chutes",
	"kind": {"kind": "Job"},
	"object": {"spec": {"template": {
		"metadata": {"labels": cargs_labels},
		"spec": object.union({"containers": [cargs_chute]}, spec_extra),
	}}},
	"userInfo": {"username": "miner"},
}

# ── the real spec sets no args anywhere, so a clean pod must pass ────────────

test_allow_chute_pod_with_no_args if {
	req := cargs_pod({})
	count({m | deny[m]; contains(m, "must not set args")}) == 0 with input as {"request": req}
}

test_allow_chute_pod_with_cache_init_and_no_args if {
	init := [{"name": "cache-init", "image": "parachutes/cache-cleaner:1"}]
	req := cargs_pod({"initContainers": init})
	count({m | deny[m]; contains(m, "must not set args")}) == 0 with input as {"request": req}
}

# ── args denied on every container position ─────────────────────────────────

test_deny_args_on_the_main_container if {
	req := cargs_pod({"containers": [{"name": "chute", "image": "i", "args": ["--evil"]}]})
	count({m | deny[m]; contains(m, "must not set args")}) > 0 with input as {"request": req}
}

test_deny_args_on_an_init_container if {
	init := [{"name": "cache-init", "image": "i", "args": ["/etc"]}]
	req := cargs_pod({"initContainers": init})
	count({m | deny[m]; contains(m, "must not set args")}) > 0 with input as {"request": req}
}

test_deny_args_on_an_ephemeral_container if {
	eph := [{"name": "debug", "image": "i", "args": ["sh"]}]
	req := cargs_pod({"ephemeralContainers": eph})
	count({m | deny[m]; contains(m, "must not set args")}) > 0 with input as {"request": req}
}

test_deny_args_on_a_job_init_container if {
	init := [{"name": "cache-init", "image": "i", "args": ["/"]}]
	req := cargs_job({"initContainers": init})
	count({m | deny[m]; contains(m, "must not set args")}) > 0 with input as {"request": req}
}

test_deny_names_the_offending_container if {
	# Both substrings in the SAME message: other rules also mention container names,
	# so matching only the name would pass even with this rule deleted.
	init := [{"name": "cache-init", "image": "i", "args": ["/etc"]}]
	req := cargs_pod({"initContainers": init})
	count({m |
		deny[m]
		contains(m, "must not set args")
		contains(m, "'cache-init'")
	}) > 0 with input as {"request": req}
}

# ── scope: only chute workloads ─────────────────────────────────────────────

test_non_chute_pod_in_namespace_may_set_args if {
	req := {
		"operation": "CREATE",
		"namespace": "chutes",
		"kind": {"kind": "Pod"},
		"object": {
			"metadata": {"labels": {"app": "opa"}},
			"spec": {"containers": [{"name": "opa", "image": "i", "args": ["run", "--server"]}]},
		},
		"userInfo": {"username": "miner"},
	}
	count({m | deny[m]; contains(m, "must not set args")}) == 0 with input as {"request": req}
}
