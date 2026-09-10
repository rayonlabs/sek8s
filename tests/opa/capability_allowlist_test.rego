# Container capabilities are an allowlist. The old 8-name denylist admitted everything outside it,
# including DAC_READ_SEARCH and the literal "ALL".
package kubernetes.admission

import future.keywords.every
import future.keywords.if
import future.keywords.in

pod_with_caps(caps) := {
	"operation": "CREATE",
	"namespace": "chutes",
	"kind": {"kind": "Pod"},
	"userInfo": {"username": "system:serviceaccount:chutes:miner"},
	"object": {
		"metadata": {"labels": {"chutes/chute": "true"}},
		"spec": {
			"securityContext": {"runAsUser": 1000},
			"containers": [{
				"name": "chute",
				"image": "registry.chutes.ai/org/chute:latest",
				"securityContext": {"capabilities": {"add": caps}},
			}],
		},
	},
}

init_pod_with_caps(caps) := {
	"operation": "CREATE",
	"namespace": "chutes",
	"kind": {"kind": "Pod"},
	"userInfo": {"username": "system:serviceaccount:chutes:miner"},
	"object": {
		"metadata": {"labels": {"chutes/chute": "true"}},
		"spec": {
			"securityContext": {"runAsUser": 1000},
			"containers": [{"name": "chute", "image": "registry.chutes.ai/org/chute:latest"}],
			"initContainers": [{
				"name": "setup",
				"image": "registry.chutes.ai/org/chute:latest",
				"securityContext": {"capabilities": {"add": caps}},
			}],
		},
	},
}

# --- capabilities the old 8-name denylist silently admitted ---------------------

test_deny_capabilities_outside_the_allowlist if {
	every cap in {
		"DAC_READ_SEARCH", "DAC_OVERRIDE", "SYS_TIME", "NET_ADMIN", "SETUID",
		"SETGID", "SYS_RESOURCE", "LINUX_IMMUTABLE", "NET_RAW", "SYS_NICE",
		"AUDIT_CONTROL", "SYSLOG", "WAKE_ALARM", "BPF", "PERFMON", "CHOWN",
		"FOWNER", "MKNOD",
	} {
		count({m | deny[m]; contains(m, "dangerous capability")}) > 0 with input as {"request": pod_with_caps([cap])}
	}
}

# "ALL" is not a capability name, so exact-match against a denylist never caught it.
test_deny_literal_all if {
	every cap in {"ALL", "all", "CAP_ALL"} {
		count({m | deny[m]; contains(m, "dangerous capability")}) > 0 with input as {"request": pod_with_caps([cap])}
	}
}

# Kubernetes accepts several spellings; containerd normalises before applying.
test_deny_regardless_of_spelling if {
	every cap in {"sys_admin", "cap_sys_admin", "Sys_Admin", "dac_read_search"} {
		count({m | deny[m]; contains(m, "dangerous capability")}) > 0 with input as {"request": pod_with_caps([cap])}
	}
}

test_deny_when_mixed_with_an_allowed_capability if {
	count({m | deny[m]; contains(m, "dangerous capability")}) > 0 with input as {"request": pod_with_caps(["IPC_LOCK", "DAC_READ_SEARCH"])}
}

# --- the previously named eight must stay denied --------------------------------

test_deny_originally_listed_capabilities if {
	every cap in {
		"SYS_ADMIN", "SYS_CHROOT", "SYS_MODULE", "SYS_RAWIO",
		"SYS_PTRACE", "SYS_BOOT", "MAC_ADMIN", "MAC_OVERRIDE",
	} {
		count({m | deny[m]; contains(m, "dangerous capability")}) > 0 with input as {"request": pod_with_caps([cap])}
	}
}

# --- legitimate capabilities must still be admitted -----------------------------

# IPC_LOCK: chute containers lock model weights in memory (chutes-miner api/k8s/util.py).
# NET_BIND_SERVICE: the only addition Kubernetes' restricted PSS permits.
test_allow_permitted_capabilities if {
	every cap in {"IPC_LOCK", "NET_BIND_SERVICE", "CAP_IPC_LOCK", "ipc_lock", "net_bind_service"} {
		count({m | deny[m]; contains(m, "dangerous capability")}) == 0 with input as {"request": pod_with_caps([cap])}
	}
}

test_allow_both_permitted_capabilities_together if {
	count({m | deny[m]; contains(m, "dangerous capability")}) == 0 with input as {"request": pod_with_caps(["IPC_LOCK", "NET_BIND_SERVICE"])}
}

test_allow_pod_requesting_no_capabilities if {
	count({m | deny[m]; contains(m, "dangerous capability")}) == 0 with input as {"request": pod_with_caps([])}
}

# --- init containers are covered too --------------------------------------------

test_deny_init_container_outside_the_allowlist if {
	count({m | deny[m]; contains(m, "dangerous capability")}) > 0 with input as {"request": init_pod_with_caps(["DAC_READ_SEARCH"])}
}

test_allow_init_container_with_permitted_capability if {
	count({m | deny[m]; contains(m, "dangerous capability")}) == 0 with input as {"request": init_pod_with_caps(["IPC_LOCK"])}
}
