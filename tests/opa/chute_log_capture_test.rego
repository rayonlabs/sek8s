# OPA tests: a chute workload must stay visible to the in-guest log shipper.
#
# The miner authors the pod spec, so without these rules it could silently opt out of
# log capture — by omitting the label the shipper discovers pods with, or by naming the
# main container something other than "chute", whose log directory is the only one read.
# Run locally: make test-opa-policies
package kubernetes.admission

import future.keywords.if
import future.keywords.in

logcap_pod(labels, containers) := {
	"operation": "CREATE",
	"namespace": "chutes",
	"kind": {"kind": "Pod"},
	"object": {"metadata": {"labels": labels}, "spec": {"containers": containers}},
	"userInfo": {"username": "miner"},
}

logcap_job(labels, containers) := {
	"operation": "CREATE",
	"namespace": "chutes",
	"kind": {"kind": "Job"},
	"object": {"spec": {"template": {
		"metadata": {"labels": labels},
		"spec": {"containers": containers},
	}}},
	"userInfo": {"username": "miner"},
}

logcap_labels := {"chutes/chute": "true", "chutes/config-id": "cfg-123"}

logcap_container := [{"name": "chute", "image": "parachutes/chute:1"}]

# ── container named "chute" ──────────────────────────────────────────────────

test_allow_chute_pod_with_main_container if {
	req := logcap_pod(logcap_labels, logcap_container)
	count({m | deny[m]; contains(m, "container named 'chute'")}) == 0 with input as {"request": req}
}

test_deny_chute_pod_whose_main_container_is_renamed if {
	req := logcap_pod(logcap_labels, [{"name": "main", "image": "parachutes/chute:1"}])
	count({m | deny[m]; contains(m, "container named 'chute'")}) > 0 with input as {"request": req}
}

test_deny_chute_job_whose_main_container_is_renamed if {
	req := logcap_job(logcap_labels, [{"name": "worker", "image": "parachutes/chute:1"}])
	count({m | deny[m]; contains(m, "container named 'chute'")}) > 0 with input as {"request": req}
}

test_allow_chute_pod_with_extra_containers_alongside_chute if {
	containers := [
		{"name": "cache-init", "image": "parachutes/cache-cleaner:1"},
		{"name": "chute", "image": "parachutes/chute:1"},
	]
	req := logcap_pod(logcap_labels, containers)
	count({m | deny[m]; contains(m, "container named 'chute'")}) == 0 with input as {"request": req}
}

# ── chutes/config-id label ───────────────────────────────────────────────────

test_deny_chute_pod_without_config_id_label if {
	req := logcap_pod({"chutes/chute": "true"}, logcap_container)
	count({m | deny[m]; contains(m, "chutes/config-id")}) > 0 with input as {"request": req}
}

test_deny_chute_pod_with_empty_config_id_label if {
	# The shipper skips a pod whose config_id is falsy, so empty hides it too.
	labels := {"chutes/chute": "true", "chutes/config-id": ""}
	req := logcap_pod(labels, logcap_container)
	count({m | deny[m]; contains(m, "chutes/config-id")}) > 0 with input as {"request": req}
}

test_deny_chute_job_without_config_id_label if {
	req := logcap_job({"chutes/chute": "true"}, logcap_container)
	count({m | deny[m]; contains(m, "chutes/config-id")}) > 0 with input as {"request": req}
}

test_allow_chute_pod_with_config_id_label if {
	req := logcap_pod(logcap_labels, logcap_container)
	count({m | deny[m]; contains(m, "chutes/config-id")}) == 0 with input as {"request": req}
}

# ── scope: these rules bind chute workloads only ─────────────────────────────

test_non_chute_pod_in_namespace_is_unaffected if {
	req := logcap_pod({"app": "opa"}, [{"name": "opa", "image": "openpolicyagent/opa:1"}])
	count({m | deny[m]; contains(m, "container named 'chute'")}) == 0 with input as {"request": req}
	count({m | deny[m]; contains(m, "chutes/config-id")}) == 0 with input as {"request": req}
}
