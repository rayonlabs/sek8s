# A per-chute cache mount must match the chute_id in the pod's launch token. Admission checks
# path <-> token only; aegis verifies the signature at runtime, so a forged token is admitted here.
package kubernetes.admission

import future.keywords.if
import future.keywords.in

# Real ES256 tokens. Signatures are irrelevant to this rule; only the claims are read.
jwt_valid_abc := "eyJhbGciOiJFUzI1NiIsInR5cCI6IkpXVCJ9.eyJjaHV0ZV9pZCI6ImNodXRlLWFiYyIsInN1YiI6ImNmZy0xIiwiaXNzIjoiY2h1dGVzIiwiZXhwIjo5OTk5OTk5OTk5LCJpYXQiOjF9.X-GD8Uc04zMbAAFasq0eyxvj67trcEjgaZor02_wAmZbDBDabG-rPa4DE6e-rvwjq2McmsE8BavzB8vw-jCFYQ"

jwt_valid_other := "eyJhbGciOiJFUzI1NiIsInR5cCI6IkpXVCJ9.eyJjaHV0ZV9pZCI6ImNodXRlLW90aGVyIiwic3ViIjoiY2ZnLTIiLCJpc3MiOiJjaHV0ZXMiLCJleHAiOjk5OTk5OTk5OTksImlhdCI6MX0.bM6JUZqPgTcUOB2WrfksWia_1yXgJOmu3uK2oPFvvkFZh_fahhWM_IzkhDIGwnZBzwHq0f1y_Oni5glToZnPQw"

# Same claims as jwt_valid_abc but signed with a different key.
jwt_forged_abc := "eyJhbGciOiJFUzI1NiIsInR5cCI6IkpXVCJ9.eyJjaHV0ZV9pZCI6ImNodXRlLWFiYyIsInN1YiI6ImNmZy0xIiwiaXNzIjoiY2h1dGVzIiwiZXhwIjo5OTk5OTk5OTk5LCJpYXQiOjF9.VEPJtcpt1tkUxv1K8SDRnMNwuSvxTEPiDZLPqn4vBaiMaAmsjz2Kaxq6q4a0gb9zIMDF_ZVZ5YAXFOFse1SVHg"

jwt_expired_abc := "eyJhbGciOiJFUzI1NiIsInR5cCI6IkpXVCJ9.eyJjaHV0ZV9pZCI6ImNodXRlLWFiYyIsInN1YiI6ImNmZy0xIiwiaXNzIjoiY2h1dGVzIiwiZXhwIjoxLCJpYXQiOjF9.GVV1KV7e4tHLZL5Kcmn7par6HepKQmcJK6DTyO_mWB2ssSF_QS7FiLeFaJ8o3PTbCE06tBbCQD2ZIN3k0smRlQ"

jwt_chute_pod(token, cache_path) := {
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
				"command": ["chutes", "run", "x"],
				"env": [{"name": "CHUTES_LAUNCH_JWT", "value": token}],
			}],
			"volumes": [{"name": "cache", "hostPath": {"path": cache_path}}],
		},
	},
}

jwt_pod_without_token(cache_path) := {
	"operation": "CREATE",
	"namespace": "chutes",
	"kind": {"kind": "Pod"},
	"userInfo": {"username": "system:serviceaccount:chutes:miner"},
	"object": {
		"metadata": {"labels": {"chutes/chute": "true"}},
		"spec": {
			"securityContext": {"runAsUser": 1000},
			"containers": [{"name": "chute", "image": "registry.chutes.ai/org/chute:latest"}],
			"volumes": [{"name": "cache", "hostPath": {"path": cache_path}}],
		},
	},
}

jwt_mismatch(path) := sprintf(
	"Chutes namespace: cache hostPath '%s' does not match the chute_id in this pod's launch token",
	[path],
)

# --- the legitimate case ---------------------------------------------------------

test_allow_cache_path_matching_the_token_chute_id if {
	not deny[jwt_mismatch("/var/snap/cache/chute-abc")] with input as {"request": jwt_chute_pod(jwt_valid_abc, "/var/snap/cache/chute-abc")}
}

# --- the attack this closes: point at a co-resident chute's directory -------------

test_deny_cache_path_for_a_different_chute if {
	deny[jwt_mismatch("/var/snap/cache/chute-victim")] with input as {"request": jwt_chute_pod(jwt_valid_abc, "/var/snap/cache/chute-victim")}
}

# Presenting a genuine token for another chute does not authorise this pod's path either --
# the two must agree, and aegis will reject the token for the chute that is not running.
test_deny_when_token_names_another_chute if {
	deny[jwt_mismatch("/var/snap/cache/chute-abc")] with input as {"request": jwt_chute_pod(jwt_valid_other, "/var/snap/cache/chute-abc")}
}

test_deny_unparseable_token if {
	deny[jwt_mismatch("/var/snap/cache/chute-abc")] with input as {"request": jwt_chute_pod("not-a-jwt", "/var/snap/cache/chute-abc")}
}

# Fail closed: a chute pod mounting a cache directory must say which chute it is.
test_deny_when_no_token_is_present if {
	deny[jwt_mismatch("/var/snap/cache/chute-abc")] with input as {"request": jwt_pod_without_token("/var/snap/cache/chute-abc")}
}

# --- deliberately NOT enforced here -----------------------------------------------

# A forged signature is admitted at this layer: the claims are consistent, so path <-> token holds.
# aegis rejects it at runtime (require_valid_launch_jwt -> fatal_security_violation), and with no
# execution path outside the signed entrypoint nothing runs to read the mount. If an exec path is
# ever reintroduced, this test is the one that must flip.
test_forged_signature_is_admitted_and_left_to_aegis if {
	not deny[jwt_mismatch("/var/snap/cache/chute-abc")] with input as {"request": jwt_chute_pod(jwt_forged_abc, "/var/snap/cache/chute-abc")}
}

# exp is not checked: a controller re-creating a pod from an unchanged spec must not be denied.
test_allow_expired_token_with_matching_chute_id if {
	not deny[jwt_mismatch("/var/snap/cache/chute-abc")] with input as {"request": jwt_chute_pod(jwt_expired_abc, "/var/snap/cache/chute-abc")}
}
