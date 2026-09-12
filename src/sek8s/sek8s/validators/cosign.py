import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Awaitable, Callable, Dict, List, Optional, Set

from sek8s.clients.cosign import (
    CosignClient,
    CosignRateLimitError,
    CosignVerificationUnavailableError,
)
from sek8s.config import AdmissionConfig, CosignConfig, CosignVerificationConfig
from sek8s.image_utils import (
    is_digest_pinned_reference,
    parse_image_reference,
    strip_tag,
)
from sek8s.validators.base import ValidationResult, ValidatorBase

logger = logging.getLogger(__name__)

# Re-export for backward compatibility
RateLimitError = CosignRateLimitError

# When upstream registry returns 429, pause all cosign calls for this long.
_RATE_LIMIT_BACKOFF_SECONDS = 300

# Transient errors (DNS, connection refused) are cached for a short window so we
# don't spam a broken endpoint, but retry quickly once it recovers.
_TRANSIENT_CACHE_SECONDS = 30

# Hard cap on verify cache entries to bound memory.
_CACHE_MAX_SIZE = 2048


@dataclass
class _CacheEntry:
    """Cached cosign outcome: digest success/failure, tag failure only, or transient error."""

    valid: Optional[bool]
    error: Optional[str]
    expires_at: float

    @property
    def expired(self) -> bool:
        return time.monotonic() >= self.expires_at


@dataclass
class _TagVerification:
    """Cached tag -> digest mapping from a successful cosign verification.

    Only populated for images on the ``pin_digest_whitelist``.  The mutating
    webhook reads this to decide whether to pin an image to a digest.
    """

    digest: str
    verified_at: float
    ttl: float

    @property
    def expired(self) -> bool:
        return time.monotonic() >= (self.verified_at + self.ttl)


@dataclass
class ValidationContext:
    """Context passed to validation rules: config, request, and pre-extracted data.

    required_key_paths is populated in _get_rules_for_context when the rule set needs it
    (e.g. chutes namespace). Rules are generic and only read context; they are
    not aware of namespace or rule-set identity.
    """

    config: AdmissionConfig
    request: dict
    namespace: str
    images: List[str]
    cosign_config: CosignConfig
    validator: "CosignValidator"
    required_key_paths: Set[Path] = field(default_factory=set)


# Rule type: async bound method (ctx) -> list of violation strings (empty if none)
Rule = Callable[[ValidationContext], Awaitable[List[str]]]


class CosignValidator(ValidatorBase):
    """Validator that verifies container image signatures using cosign.

    Caching (key = full image string):

    - **Digest-pinned** — cache success and failure with configured TTLs.
    - **Tag-only** — never cache success (tag can move). Cache **invalid** signature
      results for ``tag_failure_cache_ttl_seconds`` so kube retries do not hammer
      the registry. Set TTL to ``0`` to disable.
    - **Whitelisted tag-only** — after a successful verify, store the verified
      digest in ``_tag_cache`` with a per-image TTL.  The mutating webhook reads
      ``get_pinned_digest`` to pin pod images to that digest, and the subsequent
      validating pass hits the digest-pinned cache for zero Docker Hub calls.
    - **Transient** errors (any ref) — short TTL to avoid spamming a broken upstream.

    Upstream HTTP 429 from cosign/registry triggers a global cooldown (reactive only;
    there is no proactive admission-side RPM throttle).
    """

    def __init__(self, config: AdmissionConfig):
        super().__init__(config)
        self.cosign_config = CosignConfig()
        self._cosign_client = CosignClient()
        self._cache: Dict[str, _CacheEntry] = {}
        self._tag_cache: Dict[str, _TagVerification] = {}
        self._rate_limited_until = 0.0

    # ------------------------------------------------------------------
    # Rule sets
    # ------------------------------------------------------------------

    @property
    def _chutes_rules(self) -> List[Rule]:
        """Rule set for chutes namespace: require config, key, and verify."""
        return [
            self._require_cosign_config,
            self._reject_disabled,
            self._require_key_verification,
            self._require_ctx_key,
            self._verify_cosign_config,
        ]

    @property
    def _default_rules(self) -> List[Rule]:
        """Rule set for other namespaces: verify when config exists and not disabled."""
        return [self._verify_cosign_config]

    def _get_rules_for_context(self, ctx: ValidationContext) -> List[Rule]:
        """Return the rule set to run for the given validation context.

        Builds the union of rule sets for the context and deduplicates so rule sets
        can overlap without running the same rule twice. Order of rules does not
        affect the outcome (allow/deny or which violations are found), only the
        order of messages in the denial string.
        """
        rules: set = set()
        if ctx.namespace == "chutes":
            ctx.required_key_paths.add(self.config.chutes_public_key_path)
            ctx.required_key_paths.add(self.config.dockerhub_public_key_path)
            rules.update(self._chutes_rules)

        rules.update(self._default_rules)

        return list(rules)

    # ------------------------------------------------------------------
    # Validate entry point
    # ------------------------------------------------------------------

    async def validate(self, admission_review: Dict) -> ValidationResult:
        """Validate admission request: for pod-like resources with images,
        require valid cosign signatures; allow otherwise."""
        request = admission_review.get("request", {})

        kind = request.get("kind", {}).get("kind", "")
        if kind not in [
            "Pod",
            "Deployment",
            "StatefulSet",
            "DaemonSet",
            "Job",
            "CronJob",
            "ReplicaSet",
        ]:
            return ValidationResult.allow()

        if request.get("operation") == "DELETE":
            return ValidationResult.allow()

        obj = request.get("object", {})
        images = self.extract_images(obj)
        namespace = request.get("namespace", "default")

        pod_name = obj.get("metadata", {}).get("name", "Unknown")
        logger.debug(f"Found {len(images)} images for pod {pod_name}")

        if not images:
            return ValidationResult.allow()

        ctx = ValidationContext(
            config=self.config,
            request=request,
            namespace=namespace,
            images=images,
            cosign_config=self.cosign_config,
            validator=self,
        )
        rules = self._get_rules_for_context(ctx)

        violations: List[str] = []
        for rule in rules:
            try:
                violations.extend(await rule(ctx))
            except CosignVerificationUnavailableError as e:
                logger.warning(f"Cosign verification unavailable (network/infra): {e}")
                return ValidationResult.deny(
                    f"Cosign verification unavailable (network/infra): {e}"
                )
            except RateLimitError as e:
                logger.warning(f"Rate limited: {e}")
                violations.append(str(e))
                break
            except Exception as e:
                logger.exception(f"Rule {getattr(rule, '__name__', rule)} failed")
                violations.append(f"Verification failed: {str(e)}")

        if violations:
            return ValidationResult.deny("; ".join(violations))
        return ValidationResult.allow()

    # ------------------------------------------------------------------
    # Generic rules
    # ------------------------------------------------------------------

    async def _require_cosign_config(self, ctx: ValidationContext) -> List[str]:
        """Report any image that has no cosign configuration (used in rule sets that require config for all images)."""
        violations: List[str] = []
        seen: set = set()
        for image in ctx.images:
            if image in seen:
                continue
            seen.add(image)
            registry, org, repo, _ = parse_image_reference(image)
            vc = ctx.cosign_config.get_verification_config(registry, org, repo)
            if not vc:
                violations.append(f"Image {image} has no cosign configuration")
        return violations

    async def _reject_disabled(self, ctx: ValidationContext) -> List[str]:
        """Report any image that has verification disabled (used in rule sets that require verification)."""
        violations: List[str] = []
        seen: set = set()
        for image in ctx.images:
            if image in seen:
                continue
            seen.add(image)
            registry, org, repo, _ = parse_image_reference(image)
            vc = ctx.cosign_config.get_verification_config(registry, org, repo)
            if vc and (
                vc.verification_method == "disabled" or not vc.require_signature
            ):
                violations.append(f"Image {image} has verification disabled")
        return violations

    async def _require_key_verification(self, ctx: ValidationContext) -> List[str]:
        """Report any image not using key-based verification (used in rule sets that require a key)."""
        violations: List[str] = []
        seen: set = set()
        for image in ctx.images:
            if image in seen:
                continue
            seen.add(image)
            registry, org, repo, _ = parse_image_reference(image)
            vc = ctx.cosign_config.get_verification_config(registry, org, repo)
            if vc and (vc.verification_method != "key" or vc.public_key is None):
                violations.append(f"Image {image} must use key-based verification")
        return violations

    async def _require_ctx_key(self, ctx: ValidationContext) -> List[str]:
        """Report any image whose cosign key path is not in ctx.required_key_paths.
        Raises if required_key_paths is empty."""
        if not ctx.required_key_paths:
            raise RuntimeError(
                f"You can not use the require context key rule without providing key paths.\n"
                f"{ctx.namespace=} {ctx.required_key_paths=} {ctx.images=}"
            )
        violations: List[str] = []
        seen: set = set()
        for image in ctx.images:
            if image in seen:
                continue
            seen.add(image)
            registry, org, repo, _ = parse_image_reference(image)
            vc = ctx.cosign_config.get_verification_config(registry, org, repo)
            if (
                vc
                and vc.public_key is not None
                and vc.public_key not in ctx.required_key_paths
            ):
                violations.append(f"Image {image} uses a different cosign key")
        return violations

    async def _verify_cosign_config(self, ctx: ValidationContext) -> List[str]:
        """Verify signatures for images that have verification config enabled;
        skip images with no config or verification disabled."""
        violations: List[str] = []
        seen: set = set()
        obj_meta = ctx.request.get("object", {}).get("metadata", {})
        resource_kind = ctx.request.get("kind", {}).get("kind", "Unknown")
        resource_name = obj_meta.get("name") or obj_meta.get("generateName", "unknown")

        for image in ctx.images:
            if image in seen:
                continue
            seen.add(image)
            registry, org, repo, _ = parse_image_reference(image)
            logger.debug(
                f"Parsed image {image} -> registry={registry}, org={org}, repo={repo}"
            )
            vc = ctx.cosign_config.get_verification_config(registry, org, repo)
            if not vc:
                logger.warning(
                    f"No cosign configuration found for {registry}/{org}/{repo}, skipping verification"
                )
                continue
            if vc.verification_method == "disabled" or not vc.require_signature:
                logger.debug(
                    f"Signature verification disabled for {registry}/{org}/{repo}"
                )
                continue
            try:
                is_valid = await ctx.validator._verify_image_signature(
                    image,
                    vc,
                    resource_kind=resource_kind,
                    resource_name=resource_name,
                    namespace=ctx.namespace,
                )
                if not is_valid:
                    violations.append(
                        f"Image {image} has invalid or missing signature (registry: {registry}, org: {org})"
                    )
            except CosignVerificationUnavailableError:
                raise
            except RateLimitError:
                raise
            except Exception as e:
                logger.error(f"Error verifying image {image}: {e}")
                violations.append(f"Verification failed for {image}: {str(e)}")
        return violations

    # ------------------------------------------------------------------
    # Core verify + cache
    # ------------------------------------------------------------------

    def _read_cache(self, image: str, digest_pinned: bool) -> Optional[bool]:
        """Return cached bool result, or None if miss. Raises on cached transient error."""
        entry = self._cache.get(image)
        if not entry or entry.expired:
            return None
        if entry.valid is None:
            raise CosignVerificationUnavailableError(entry.error or "")
        if entry.valid is False:
            logger.info(f"Cosign cache hit for {image} (cached invalid signature)")
            return False
        if digest_pinned:
            logger.info(f"Cosign cache hit for {image} (valid=True)")
            return True
        return None

    async def _verify_image_signature(
        self,
        image: str,
        verification_config: CosignVerificationConfig,
        resource_kind: str = "Unknown",
        resource_name: str = "unknown",
        namespace: str = "default",
    ) -> bool:
        """Verify image signature with registry-friendly caching (see class docstring)."""
        digest_pinned = is_digest_pinned_reference(image)

        cached = self._read_cache(image, digest_pinned)
        if cached is not None:
            return cached

        if self._rate_limited_until and time.monotonic() < self._rate_limited_until:
            raise RateLimitError(
                "Cosign verification paused due to upstream rate limiting"
            )

        try:
            valid, verified_digest = await self._cosign_client.verify(
                image, verification_config, timeout=60.0
            )
        except CosignVerificationUnavailableError as e:
            self._put(image, None, str(e), _TRANSIENT_CACHE_SECONDS)
            raise
        except CosignRateLimitError:
            self._rate_limited_until = time.monotonic() + _RATE_LIMIT_BACKOFF_SECONDS
            raise
        except Exception as e:
            logger.error(f"cosign verify exception for {image}: {e}")
            valid = False
            verified_digest = None

        if digest_pinned:
            ttl = (
                self.cosign_config.success_cache_ttl_seconds
                if valid
                else self.cosign_config.failure_cache_ttl_seconds
            )
            self._put(image, valid, None, float(ttl))
        elif not valid:
            ttl = self.cosign_config.tag_failure_cache_ttl_seconds
            if ttl > 0:
                self._put(image, False, None, float(ttl))
        elif valid and verified_digest:
            image_no_tag = strip_tag(image)
            pin_ttl = self.cosign_config.get_pin_ttl(image_no_tag)
            if pin_ttl is not None:
                self._tag_cache[image] = _TagVerification(
                    digest=verified_digest,
                    verified_at=time.monotonic(),
                    ttl=float(pin_ttl),
                )
                digest_ref = f"{image_no_tag}@{verified_digest}"
                self._put(digest_ref, True, None, float(pin_ttl))
                logger.info(
                    "Cached tag pin: %s -> %s (TTL %ds)",
                    image,
                    verified_digest,
                    pin_ttl,
                )

        return valid

    def get_pinned_digest(self, image: str) -> Optional[str]:
        """Return a verified digest for a whitelisted tag-only image if within TTL.

        Called by the mutating webhook to decide whether to pin the image to a
        specific digest.  Returns ``None`` if the image is not whitelisted, has
        never been verified, or the TTL has expired.
        """
        entry = self._tag_cache.get(image)
        if entry and not entry.expired:
            return entry.digest
        return None

    def get_stats(self) -> dict:
        """Return cache and tag-pin stats."""
        active_pins = {
            img: {
                "digest": tv.digest,
                "remaining_s": round(tv.ttl - (time.monotonic() - tv.verified_at), 1),
            }
            for img, tv in self._tag_cache.items()
            if not tv.expired
        }
        return {
            "cache_size": len(self._cache),
            "tag_pin_cache_size": len(active_pins),
            "active_tag_pins": active_pins,
            "rate_limited_until": (
                self._rate_limited_until - time.monotonic()
                if self._rate_limited_until
                and time.monotonic() < self._rate_limited_until
                else 0
            ),
        }

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _put(
        self,
        key: str,
        valid: Optional[bool],
        error: Optional[str],
        ttl: float,
    ) -> None:
        if len(self._cache) >= _CACHE_MAX_SIZE:
            now = time.monotonic()
            self._cache = {k: v for k, v in self._cache.items() if v.expires_at > now}
        self._cache[key] = _CacheEntry(
            valid=valid, error=error, expires_at=time.monotonic() + ttl
        )
