# Chute workloads may not use projected volumes: projected sources include serviceAccountToken,
# which bypasses automountServiceAccountToken: false and hands untrusted code a kube API token.
package kubernetes.admission

import future.keywords.if
import future.keywords.in

proj_sa_token := {"name": "kube-api-access", "projected": {"sources": [
	{"serviceAccountToken": {"expirationSeconds": 3607, "path": "token"}},
	{"configMap": {"name": "kube-root-ca.crt"}},
]}}

proj_secret := {"name": "creds", "projected": {"sources": [{"secret": {"name": "miner-credentials"}}]}}

proj_msg(name) := sprintf(
	"Chutes namespace: chute workload may not use a projected volume ('%s'); projected sources include serviceAccountToken, which bypasses automountServiceAccountToken: false",
	[name],
)

proj_pod(labels, volumes) := {
	"operation": "CREATE",
	"namespace": "chutes",
	"kind": {"kind": "Pod"},
	"userInfo": {"username": "system:serviceaccount:chutes:miner"},
	"object": {
		"metadata": {"labels": labels},
		"spec": {
			"securityContext": {"runAsUser": 1000},
			"automountServiceAccountToken": false,
			"containers": [{"name": "chute", "image": "registry.chutes.ai/org/chute:latest"}],
			"volumes": volumes,
		},
	},
}

# --- the regression: a chute pod smuggling an API token ---------------------------

test_deny_chute_pod_projected_service_account_token if {
	deny[proj_msg("kube-api-access")] with input as {"request": proj_pod({"chutes/chute": "true"}, [proj_sa_token])}
}

# automountServiceAccountToken: false does not help — it only suppresses the auto-injected volume.
test_deny_even_when_automount_is_false if {
	req := proj_pod({"chutes/chute": "true"}, [proj_sa_token])
	req.object.spec.automountServiceAccountToken == false
	deny[proj_msg("kube-api-access")] with input as {"request": req}
}

test_deny_chute_pod_projected_secret if {
	deny[proj_msg("creds")] with input as {"request": proj_pod({"chutes/chute": "true"}, [proj_secret])}
}

test_deny_projected_in_a_chute_job_template if {
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
				"volumes": [proj_sa_token],
			},
		}}},
	}
	deny[proj_msg("kube-api-access")] with input as {"request": req}
}

# --- the legitimate consumer must keep working ------------------------------------
# failed-chute-cleanup is deployed into the guest by sek8s and needs a serviceAccountToken source.
# It is not labelled chutes/chute=true.

test_allow_projected_on_a_non_chute_pod if {
	not deny[proj_msg("kube-api-access")] with input as {"request": proj_pod({"app": "failed-chute-cleanup"}, [proj_sa_token])}
}

# --- the volumes a chute actually uses stay allowed --------------------------------

test_allow_chute_pod_hostpath_and_emptydir if {
	req := proj_pod({"chutes/chute": "true"}, [
		{"name": "cache", "hostPath": {"path": "/var/snap/cache/abc"}},
		{"name": "tmp", "emptyDir": {}},
	])
	count({m | deny[m]; contains(m, "projected volume")}) == 0 with input as {"request": req}
}

# --- serviceAccountName: the other half of the escalation --------------------------
# The `agent` SA holds secrets get/list/watch in chutes, and miner-credentials (the seed) lives
# there. A chute naming that SA plus a projected token source was a direct read of the seed.

sa_msg(name) := sprintf(
	"Chutes namespace: chute workload may not select serviceAccountName '%s'; chutes run as the unprivileged default account",
	[name],
)

sa_pod(labels, sa) := {
	"operation": "CREATE",
	"namespace": "chutes",
	"kind": {"kind": "Pod"},
	"userInfo": {"username": "system:serviceaccount:chutes:miner"},
	"object": {
		"metadata": {"labels": labels},
		"spec": object.union(
			{
				"securityContext": {"runAsUser": 1000},
				"containers": [{"name": "chute", "image": "registry.chutes.ai/org/chute:latest"}],
				"volumes": [],
			},
			sa,
		),
	},
}

test_deny_chute_pod_naming_the_agent_service_account if {
	deny[sa_msg("agent")] with input as {"request": sa_pod({"chutes/chute": "true"}, {"serviceAccountName": "agent"})}
}

test_deny_chute_pod_naming_any_non_default_service_account if {
	every sa in {"agent", "chutes", "miner"} {
		deny[sa_msg(sa)] with input as {"request": sa_pod({"chutes/chute": "true"}, {"serviceAccountName": sa})}
	}
}

# The real spec sets none, so the pod runs as `default`.
test_allow_chute_pod_with_no_service_account_name if {
	count({m | deny[m]; contains(m, "serviceAccountName")}) == 0 with input as {"request": sa_pod({"chutes/chute": "true"}, {})}
}

test_allow_chute_pod_explicitly_naming_default if {
	count({m | deny[m]; contains(m, "serviceAccountName")}) == 0 with input as {"request": sa_pod({"chutes/chute": "true"}, {"serviceAccountName": "default"})}
}

# Non-chute pods in the namespace keep choosing their account (cleanup uses `chutes`).
test_allow_non_chute_pod_naming_a_service_account if {
	count({m | deny[m]; contains(m, "serviceAccountName")}) == 0 with input as {"request": sa_pod({"app": "failed-chute-cleanup"}, {"serviceAccountName": "chutes"})}
}
