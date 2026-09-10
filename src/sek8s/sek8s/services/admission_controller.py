#!/usr/bin/env python3
"""
TEE K3s Admission Controller with OPA Integration
Phase 4a - Basic Python + OPA
"""

import asyncio
import base64
import json
import logging
import time
from typing import Any, Dict, List, Optional

import orjson
from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.responses import Response

from sek8s.config import AdmissionConfig
from sek8s.image_utils import is_digest_pinned_reference, strip_tag
from sek8s.server import WebServer
from sek8s.services.admission_models import (
    AdmissionResponseBody,
    AdmissionReviewResponse,
    AdmissionStatus,
    SubjectAccessReviewResponse,
)
from sek8s.validators.base import ValidatorBase
from sek8s.validators.cosign import CosignValidator
from sek8s.validators.opa import OPAValidator
from sek8s.validators.registry import RegistryValidator

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

_ACCESS_LOG_INTERVAL_SECONDS = 60

# Pre-serialized SubjectAccessReview "no opinion" response.  Returned for the
# vast majority of authorize requests; avoids Pydantic validation/serialization
# and JSON encoding on every call (~16 req/s at steady state).
_NO_OPINION_BODY: bytes = orjson.dumps(
    {
        "apiVersion": "authorization.k8s.io/v1",
        "kind": "SubjectAccessReview",
        "status": {"allowed": False, "denied": False},
    }
)


class _BatchAccessLog(logging.Filter):
    """Suppress individual 'POST /validate 200', 'POST /mutate 200', and
    'POST /authorize 200' uvicorn access-log lines and instead emit a single
    count every 60 seconds."""

    def __init__(self) -> None:
        super().__init__()
        self._validate_ok: int = 0
        self._mutate_ok: int = 0
        self._authorize_ok: int = 0
        self._last_flush = time.monotonic()

    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        is_ok = "200" in msg
        suppress = False

        if is_ok and "/validate" in msg:
            self._validate_ok += 1
            suppress = True
        elif is_ok and "/mutate" in msg:
            self._mutate_ok += 1
            suppress = True
        elif is_ok and "/authorize" in msg:
            self._authorize_ok += 1
            suppress = True

        self._maybe_flush()
        return not suppress

    def _maybe_flush(self) -> None:
        now = time.monotonic()
        if now - self._last_flush < _ACCESS_LOG_INTERVAL_SECONDS:
            return
        self._last_flush = now
        total = self._validate_ok + self._mutate_ok + self._authorize_ok
        if total > 0:
            logger.info(
                "Webhooks: %d requests in last %ds (validate=%d, mutate=%d, authorize=%d)",
                total,
                _ACCESS_LOG_INTERVAL_SECONDS,
                self._validate_ok,
                self._mutate_ok,
                self._authorize_ok,
            )
        self._validate_ok = 0
        self._mutate_ok = 0
        self._authorize_ok = 0


logging.getLogger("uvicorn.access").addFilter(_BatchAccessLog())


class AdmissionController:
    """Main admission controller that orchestrates validation."""

    def __init__(self, config: AdmissionConfig):
        self.config = config

        self.validators: List[ValidatorBase] = []
        self._init_validators()

        logger.info(
            "Admission controller initialized with %d validators", len(self.validators)
        )

    def _init_validators(self):
        """Initialize all configured validators."""
        self.validators.append(OPAValidator(self.config))
        self.validators.append(RegistryValidator(self.config))

        cosign = CosignValidator(self.config)
        self.validators.append(cosign)
        self._cosign_validator = cosign

        logger.info(
            "Initialized validators: %s",
            [v.__class__.__name__ for v in self.validators],
        )

    async def validate_admission(
        self, admission_review: Dict
    ) -> AdmissionReviewResponse:
        """
        Main validation entry point.

        Args:
            admission_review: Kubernetes admission review request

        Returns:
            Admission review response
        """
        start_time = time.time()
        request = admission_review.get("request", {})
        uid = request.get("uid", "unknown")
        kind = request.get("kind", {}).get("kind", "unknown")
        operation = request.get("operation", "unknown")
        namespace = request.get("namespace", "default")

        logger.debug(
            "Processing admission request: uid=%s, kind=%s, operation=%s, namespace=%s",
            uid,
            kind,
            operation,
            namespace,
        )

        try:
            validation_tasks = [
                validator.validate(admission_review) for validator in self.validators
            ]

            results = await asyncio.gather(*validation_tasks, return_exceptions=True)

            allowed = True
            messages: list[str] = []
            warnings: list[str] = []

            for i, result in enumerate(results):
                validator_name = self.validators[i].__class__.__name__

                if isinstance(result, BaseException):
                    logger.error("Validator %s failed: %s", validator_name, result)
                    allowed = False
                    messages.append(f"{validator_name}: Internal error")
                    continue

                if not result.allowed:
                    allowed = False
                    messages.extend(result.messages)

                warnings.extend(result.warnings)

                logger.debug(
                    "Validator %s: allowed=%s, messages=%d, warnings=%d",
                    validator_name,
                    result.allowed,
                    len(result.messages),
                    len(result.warnings),
                )

            response = self._build_response(
                uid=uid, allowed=allowed, messages=messages, warnings=warnings
            )

            elapsed = time.time() - start_time

            logger.debug(
                "Admission decision for %s: allowed=%s, duration=%.3fs",
                uid,
                allowed,
                elapsed,
            )

            return response

        except Exception as e:
            logger.exception("Unexpected error processing admission request %s", uid)

            return self._build_response(
                uid=uid,
                allowed=False,
                messages=[f"Internal error: {str(e)}"],
                warnings=[],
            )

    def _build_response(
        self, uid: str, allowed: bool, messages: List[str], warnings: List[str]
    ) -> AdmissionReviewResponse:
        """Build admission review response."""
        body = AdmissionResponseBody(
            uid=uid,
            allowed=allowed,
            status=AdmissionStatus(message="; ".join(messages)) if messages else None,
            warnings=warnings or None,
        )
        return AdmissionReviewResponse(response=body)

    async def health_check(self) -> Dict:
        """Check health of all validators."""
        health_status: Dict[str, Any] = {"healthy": True, "validators": {}}

        for validator in self.validators:
            try:
                is_healthy = await validator.health_check()
                health_status["validators"][validator.__class__.__name__] = {
                    "healthy": is_healthy
                }
                if not is_healthy:
                    health_status["healthy"] = False
            except Exception as e:
                health_status["validators"][validator.__class__.__name__] = {
                    "healthy": False,
                    "error": str(e),
                }
                health_status["healthy"] = False

        return health_status

    def evaluate_authorization_raw(
        self,
        spec: Dict[str, Any],
        resource_attrs: Optional[Dict[str, Any]],
    ) -> SubjectAccessReviewResponse:
        """Core authorization logic operating on plain dicts.

        Enforces an allowlist for miner pod log access in the chutes namespace.
        Returns NoOpinion for everything outside scope; returns Denied for
        miner log requests not matching an allowed pod name prefix.

        The handler calls this directly with orjson-parsed dicts to avoid
        Pydantic model construction on the hot path.
        """
        if not resource_attrs:
            return SubjectAccessReviewResponse.no_opinion()

        is_miner_chutes_log = (
            spec.get("user") == "miner"
            and resource_attrs.get("resource") == "pods"
            and resource_attrs.get("subresource") == "log"
            and resource_attrs.get("namespace") == "chutes"
        )

        if not is_miner_chutes_log:
            return SubjectAccessReviewResponse.no_opinion()

        name = resource_attrs.get("name", "")
        is_allowed_pod = any(
            name.startswith(p) for p in self.config.authz_allowed_log_prefixes
        )

        if is_allowed_pod:
            return SubjectAccessReviewResponse.no_opinion()

        logger.info(
            "Authorization denied: miner pods/log for %s/%s",
            resource_attrs.get("namespace"),
            name,
        )
        return SubjectAccessReviewResponse.denied(
            f"Log access denied for pod '{name}' "
            f"in namespace '{resource_attrs.get('namespace')}'"
        )

    def build_image_pin_patches(self, request: dict) -> List[dict]:
        """Build JSON Patch operations to pin whitelisted tag-only images to their verified digest.

        Returns an empty list when there is nothing to mutate (no cosign
        validator, no whitelisted images, or no cached digest).
        """

        obj = request.get("object", {})
        patches: List[dict] = []

        def _maybe_pin(containers: List[dict], json_prefix: str) -> None:
            for idx, container in enumerate(containers):
                image = container.get("image", "")
                if not image or is_digest_pinned_reference(image):
                    continue
                digest = self._cosign_validator.get_pinned_digest(image)
                if digest:
                    image_no_tag = strip_tag(image)
                    pinned = f"{image_no_tag}@{digest}"
                    patches.append(
                        {
                            "op": "replace",
                            "path": f"{json_prefix}/{idx}/image",
                            "value": pinned,
                        }
                    )

        spec = obj.get("spec", {})

        # Direct pod spec
        if "containers" in spec:
            _maybe_pin(spec.get("containers", []), "/spec/containers")
        if "initContainers" in spec:
            _maybe_pin(spec.get("initContainers", []), "/spec/initContainers")

        # Deployment / StatefulSet / DaemonSet / Job template
        tpl_spec = spec.get("template", {}).get("spec", {})
        if tpl_spec:
            if "containers" in tpl_spec:
                _maybe_pin(tpl_spec["containers"], "/spec/template/spec/containers")
            if "initContainers" in tpl_spec:
                _maybe_pin(
                    tpl_spec["initContainers"], "/spec/template/spec/initContainers"
                )

        # CronJob extra nesting
        jt_spec = (
            spec.get("jobTemplate", {})
            .get("spec", {})
            .get("template", {})
            .get("spec", {})
        )
        if jt_spec:
            if "containers" in jt_spec:
                _maybe_pin(
                    jt_spec["containers"],
                    "/spec/jobTemplate/spec/template/spec/containers",
                )
            if "initContainers" in jt_spec:
                _maybe_pin(
                    jt_spec["initContainers"],
                    "/spec/jobTemplate/spec/template/spec/initContainers",
                )

        return patches


class AdmissionWebhookServer(WebServer):
    """Async web server for admission webhook."""

    body_hash_skip_paths = frozenset({"/authorize"})

    def __init__(self, config: AdmissionConfig):
        self.controller = AdmissionController(config)
        super().__init__(config)

    def _setup_routes(self):
        """Setup web routes."""
        self.app.add_api_route("/validate", self.handle_validate, methods=["POST"])
        self.app.add_api_route("/mutate", self.handle_mutate, methods=["POST"])
        self.app.add_api_route("/authorize", self.handle_authorize, methods=["POST"])
        self.app.add_api_route("/health", self.handle_health, methods=["GET"])
        self.app.add_api_route("/ready", self.handle_ready, methods=["GET"])

    async def handle_validate(self, request: Request) -> JSONResponse:
        """Handle validation webhook requests."""
        try:
            admission_review = await request.json()

            if not admission_review.get("request"):
                return JSONResponse(
                    content={"error": "Invalid admission review: missing request"},
                    status_code=400,
                )

            response = await self.controller.validate_admission(admission_review)
            return JSONResponse(content=response.model_dump(exclude_none=True))

        except json.JSONDecodeError as e:
            logger.error("Invalid JSON in request: %s", e)
            return JSONResponse(
                content={"error": "Invalid JSON"},
                status_code=400,
            )
        except Exception as e:
            logger.exception("Error handling validation request")

            uid = admission_review.get("request", {}).get("uid", "unknown")
            error_response = AdmissionReviewResponse(
                response=AdmissionResponseBody(
                    uid=uid,
                    allowed=False,
                    status=AdmissionStatus(message=f"Internal server error: {str(e)}"),
                )
            )
            return JSONResponse(content=error_response.model_dump(exclude_none=True))

    async def handle_mutate(self, request: Request) -> JSONResponse:
        """Handle mutation webhook requests.

        Currently applies:
        - Image digest pinning for whitelisted images with a cached verified
          digest (tag-only references are replaced with their digest).
        - automountServiceAccountToken: false for all pods in chutes namespace
          (chute workloads have no reason to access the k8s API)
        """
        try:
            request_data = await request.json()
            req = request_data.get("request", {})
            uid = req.get("uid", "unknown")
            namespace = req.get("namespace", "")
            operation = req.get("operation", "")
            kind = req.get("kind", {}).get("kind", "")

            patches = self.controller.build_image_pin_patches(req)

            # automountServiceAccountToken is immutable on existing Pods.
            can_patch_sa_token = not (kind == "Pod" and operation != "CREATE")
            if namespace == "chutes" and can_patch_sa_token:
                patches.extend(self._build_sa_token_patches(req))

            patch_type: Optional[str] = None
            patch_data: Optional[str] = None
            if patches:
                patch_type = "JSONPatch"
                patch_data = base64.b64encode(json.dumps(patches).encode()).decode()
                logger.info(
                    "Mutating webhook: applied %d patch(es) for %s/%s",
                    len(patches),
                    namespace or "?",
                    req.get(
                        "name",
                        req.get("object", {})
                        .get("metadata", {})
                        .get("generateName", "?"),
                    ),
                )

            response = AdmissionReviewResponse(
                response=AdmissionResponseBody(
                    uid=uid,
                    allowed=True,
                    patchType=patch_type,
                    patch=patch_data,
                )
            )
            return JSONResponse(content=response.model_dump(exclude_none=True))
        except json.JSONDecodeError as e:
            logger.error("Invalid JSON in mutation request: %s", e)
            return JSONResponse(
                content={"error": "Invalid JSON"},
                status_code=400,
            )
        except Exception:
            logger.exception("Error handling mutation request")
            error_response = AdmissionReviewResponse(
                response=AdmissionResponseBody(uid="unknown", allowed=True)
            )
            return JSONResponse(content=error_response.model_dump(exclude_none=True))

    async def handle_authorize(self, request: Request) -> Response:
        """Handle authorization webhook requests (SubjectAccessReview).

        Uses orjson for parsing and a pre-serialized no_opinion response for
        the common path.  All decision logic lives in the controller's
        ``evaluate_authorization_raw``.
        """
        try:
            data = orjson.loads(await request.body())
            spec = data.get("spec") or {}
            attrs = spec.get("resourceAttributes")

            response = self.controller.evaluate_authorization_raw(spec, attrs)

            if not response.status.denied:
                return Response(content=_NO_OPINION_BODY, media_type="application/json")

            return JSONResponse(content=response.model_dump(exclude_none=True))

        except Exception:
            logger.exception("Error handling authorization request")
            denied = SubjectAccessReviewResponse.denied(
                "Authorization webhook internal error"
            )
            return JSONResponse(content=denied.model_dump(exclude_none=True))

    @staticmethod
    def _is_agent_workload(labels: dict, containers: list) -> bool:
        """Check if the workload is the agent (label + image match).

        The OPA validating webhook enforces the full exemption (including
        userInfo checks at the Pod level); the mutator only needs to avoid
        clobbering automountServiceAccountToken on the agent.
        """
        is_agent_label = labels.get("app.kubernetes.io/name") == "agent"
        has_agent_image = any(
            c.get("image", "").startswith("parachutes/chutes-agent") for c in containers
        )
        return is_agent_label and has_agent_image

    @staticmethod
    def _build_sa_token_patches(req: dict) -> list:
        """Build JSON Patch ops to set automountServiceAccountToken: false.

        Skips the agent Deployment/Pod which legitimately needs a service
        account token for in-cluster API access.
        """
        kind = req.get("kind", {}).get("kind", "")
        obj = req.get("object", {})

        if kind == "Pod":
            labels = obj.get("metadata", {}).get("labels", {})
            spec = obj.get("spec", {})
            if AdmissionWebhookServer._is_agent_workload(
                labels, spec.get("containers", [])
            ):
                return []
            if spec.get("automountServiceAccountToken") is not False:
                return [
                    {
                        "op": "add",
                        "path": "/spec/automountServiceAccountToken",
                        "value": False,
                    }
                ]
        elif kind in ("Deployment", "StatefulSet", "DaemonSet", "ReplicaSet", "Job"):
            template = obj.get("spec", {}).get("template", {})
            labels = template.get("metadata", {}).get("labels", {})
            spec = template.get("spec", {})
            if kind != "Job" and AdmissionWebhookServer._is_agent_workload(
                labels, spec.get("containers", [])
            ):
                return []
            if spec.get("automountServiceAccountToken") is not False:
                return [
                    {
                        "op": "add",
                        "path": "/spec/template/spec/automountServiceAccountToken",
                        "value": False,
                    }
                ]
        elif kind == "CronJob":
            spec = (
                obj.get("spec", {})
                .get("jobTemplate", {})
                .get("template", {})
                .get("spec", {})
            )
            if spec.get("automountServiceAccountToken") is not False:
                return [
                    {
                        "op": "add",
                        "path": "/spec/jobTemplate/spec/template/spec/automountServiceAccountToken",
                        "value": False,
                    }
                ]

        return []

    async def handle_health(self, request: Request) -> JSONResponse:
        """Health check endpoint."""
        health_status = await self.controller.health_check()
        status_code = 200 if health_status["healthy"] else 503
        return JSONResponse(content=health_status, status_code=status_code)

    async def handle_ready(self, request: Request) -> JSONResponse:
        """Readiness check endpoint."""
        # Simple readiness for now - could check OPA connection, etc.
        health_status = await self.controller.health_check()
        if health_status["healthy"]:
            return JSONResponse(content={"ready": True})
        else:
            return JSONResponse(content={"ready": False}, status_code=503)


def run():
    """Main entry point."""
    try:
        config = AdmissionConfig()

        if config.debug:
            logging.getLogger().setLevel(logging.DEBUG)
            logger.debug("Debug mode enabled")
            logger.debug("Configuration: %s", config.export_json())

        if not config.tls_cert_path or not config.tls_key_path:
            logger.warning("TLS certificates not configured, running in insecure mode")

        server = AdmissionWebhookServer(config)
        server.run()

    except Exception as e:
        logger.exception("Failed to start admission controller: %s", e)
        raise


if __name__ == "__main__":
    run()
