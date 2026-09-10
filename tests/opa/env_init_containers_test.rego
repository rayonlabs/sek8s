# The env allowlist applies to init and ephemeral containers, not just spec.containers. cache-init
# is the one container permitted to run as uid 0, so LD_PRELOAD there is root code execution.
package kubernetes.admission

import future.keywords.if
import future.keywords.in

# The real cache-init env, as chutes-miner builds it.
env_real_cache_init := [
	{"name": "CLEANUP_EXCLUDE", "value": "chute-abc"},
	{"name": "HF_HOME", "value": "/cache"},
	{"name": "CIVITAI_HOME", "value": "/cache/civitai"},
	{"name": "CACHE_MAX_AGE_DAYS", "value": "7"},
	{"name": "CACHE_MAX_SIZE_GB", "value": "500"},
	{"name": "NVIDIA_VISIBLE_DEVICES", "value": "all"},
]

env_pod(spec_extra) := {
	"operation": "CREATE",
	"namespace": "chutes",
	"kind": {"kind": "Pod"},
	"userInfo": {"username": "system:serviceaccount:chutes:miner"},
	"object": {
		"metadata": {"labels": {"chutes/chute": "true"}},
		"spec": object.union(
			{
				"securityContext": {"runAsUser": 1000},
				"containers": [{"name": "chute", "image": "registry.chutes.ai/org/chute:latest"}],
			},
			spec_extra,
		),
	},
}

env_init(env) := env_pod({"initContainers": [{
	"name": "cache-init",
	"image": "parachutes/cache-cleaner:latest",
	"securityContext": {"runAsUser": 0},
	"env": env,
}]})

env_denied(name, var) := sprintf("Container '%s' uses forbidden environment variable '%s'", [name, var])

# --- the regression --------------------------------------------------------------

test_deny_ld_preload_on_the_root_init_container if {
	deny[env_denied("cache-init", "LD_PRELOAD")] with input as {"request": env_init([{"name": "LD_PRELOAD", "value": "/cache/evil.so"}])}
}

# Explicitly forbidden, and was reachable on an init container.
test_deny_kubeconfig_on_an_init_container if {
	deny[env_denied("cache-init", "KUBECONFIG")] with input as {"request": env_init([{"name": "KUBECONFIG", "value": "/cache/kc"}])}
}

test_deny_arbitrary_env_on_an_init_container if {
	every var in {"PYTHONPATH", "LD_LIBRARY_PATH", "BASH_ENV", "KUBE_TOKEN"} {
		deny[env_denied("cache-init", var)] with input as {"request": env_init([{"name": var, "value": "x"}])}
	}
}

test_deny_env_on_an_ephemeral_container if {
	req := env_pod({"ephemeralContainers": [{
		"name": "debug",
		"image": "registry.chutes.ai/org/chute:latest",
		"env": [{"name": "LD_PRELOAD", "value": "/cache/evil.so"}],
	}]})
	deny[env_denied("debug", "LD_PRELOAD")] with input as {"request": req}
}

test_deny_env_on_init_container_in_a_job_template if {
	req := {
		"operation": "CREATE",
		"namespace": "chutes",
		"kind": {"kind": "Job"},
		"userInfo": {"username": "system:serviceaccount:chutes:miner"},
		"object": {"spec": {"template": {
			"metadata": {"labels": {"chutes/chute": "true"}},
			"spec": {
				"securityContext": {"runAsUser": 1000},
				"containers": [{"name": "chute", "image": "registry.chutes.ai/org/chute:latest"}],
				"initContainers": [{"name": "cache-init", "image": "parachutes/cache-cleaner:latest", "env": [{"name": "LD_PRELOAD", "value": "/x.so"}]}],
			},
		}}},
	}
	deny[env_denied("cache-init", "LD_PRELOAD")] with input as {"request": req}
}

# --- the real spec must keep working ---------------------------------------------
# These names are only allowed because they were added to allowed_env_vars alongside this rule;
# without them the legitimate cache-init would be denied.

test_allow_the_real_cache_init_env if {
	count({m | deny[m]; contains(m, "forbidden environment variable")}) == 0 with input as {"request": env_init(env_real_cache_init)}
}

# --- main containers keep their existing behaviour --------------------------------

test_deny_forbidden_env_on_a_main_container if {
	req := env_pod({"containers": [{
		"name": "chute",
		"image": "registry.chutes.ai/org/chute:latest",
		"env": [{"name": "LD_PRELOAD", "value": "/x.so"}],
	}]})
	deny[env_denied("chute", "LD_PRELOAD")] with input as {"request": req}
}
