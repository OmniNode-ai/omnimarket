# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerPushValidationEffect — push-validation write-EFFECT (OMN-14920,
Contract v2 mode=validate_only OMN-14976).

Canonical def-B handler: ``handle(request: ModelPushValidationRequest) ->
ModelPushValidationReceipt``. The envelope boundary is the shared runtime
adapter; this module never references the event-envelope type. Hand-written
under the documented-exception grant of 2026-07-22 (RSD Track-2 generation not
usable this week); the contract + acceptance suite are the future RSD
regeneration target.

Contract-text semantics (load-bearing, encoded verbatim from contract.yaml):

(1) expected_head_sha is FAIL-CLOSED: after fetch, if the fetched head of
    ``branch`` != expected_head_sha, ABORT — no silent refetch, no
    retry-with-new-head, no suite, no push. Receipt: outcome=stale_head,
    suite_verdict=not_run, push_exit=None, remote_sha_readback=observed head.
(2) Suite failure is a SUCCESSFUL node execution: the receipt
    (outcome=suite_failed, push_exit=None) is emitted on the COMPLETED topic.
    The failure terminal topic is reserved for infrastructure/handler errors
    (clone failure, toolchain absent, crash) — which this handler surfaces by
    LETTING THE CLIENT'S EXCEPTIONS PROPAGATE. A seeded failing-suite request
    produces a completed-topic receipt recording the failure honestly and
    NEVER pushes — a red suite NEVER pushes.
(3) Zero bypass flags anywhere in the path; hook_id_readback (digest of the
    INSTALLED pre-push hook content) is captured BEFORE any push attempt, and
    an unhooked clone refuses to push (outcome=refused).
(4) branch in {dev, main} -> outcome=refused (the worker never pushes
    protected branches directly).
(5) Idempotency under at-least-once redelivery: if the remote head already
    contains expected_head_sha as pushed state on arrival,
    outcome=already_pushed, suite_verdict=not_run — a redelivered command must
    not double-push. This check runs FIRST, before the stale-head comparison,
    so a redelivery after a successful push short-circuits even if the live
    branch has since moved on.
(6) Contract v2 (OMN-14976): ``request.mode=validate_only`` — after a green
    suite, the handler returns outcome=validated WITHOUT calling
    ``push_branch`` at all. The push flow (mode=validate_and_push, the
    default) is completely unchanged; this is a pure addition gated on the
    new field. The request model's own invariant already guarantees
    ``source_identity`` (when present) is ``commit``-only for
    mode=validate_and_push, so the handler does not need to re-check it.

Tenant gate (optional-input-silent-skip is banned): ``tenant_principal_id``
must be present and well-formed (``t-<32hex>``). The typed request model
already enforces this at parse time; the handler re-validates defensively
(``model_construct`` bypass). A malformed principal cannot key a tenant-scoped
receipt — the receipt model's own pattern would reject it — so the handler
REFUSES by raising (failure terminal topic) rather than fabricating tenant
identity in a completed-topic receipt.

(7) Bundle transfer (OMN-14979): a ``tree`` / ``commit+patch``
    ``source_identity`` carries a REQUIRED ``bundle`` reference, because that
    state is by definition NOT on origin — the worker cannot clone what was
    never pushed. For those requests the handler takes a separate path:
    materialize the bundle, then run the suite against the UNPACKED REF.
    The origin-state checks (already_pushed, stale_head) are deliberately
    NOT applied — they describe the remote branch, not the bundle, and
    applying them would short-circuit every bundle request whose base commit
    happens to be on origin. Failure modes are honest and distinguishable
    (url_expired / oversize / checksum_mismatch / unusable_bundle) and
    surface as outcome=bundle_unavailable on the COMPLETED topic, because
    they are deterministic facts about the request; S3/transport faults RAISE
    to the failure topic instead. Tenant isolation is enforced HERE, not by
    IAM: the worker runs under a shared node role, so the handler requires
    the bundle key's tenant segment to equal ``tenant_principal_id`` and
    refuses otherwise (outcome=refused). The push flow is unreachable for a
    bundle — the request model forces mode=validate_only for every
    non-commit identity, and the handler re-asserts it rather than trusting
    it.

Named residuals NOT built this session (see OMN-14976 ticket comment): the
gateway-side (omninode_infra) advertisement of ``mode``/``source_identity``
and of the bundle ref, plus the gateway grant that would mint the presigned
PUT; ``environment_identity`` population (the receipt field exists, defaults
to ``None`` — no runtime-lane-identity source is wired into this handler
yet); the pre-push client that would actually build the bundle and set
mode=validate_only from a laptop (OMN-14980).
"""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import UTC, datetime
from typing import Literal

from omnimarket.nodes.node_push_validation_effect.models.model_push_validation_receipt import (
    EnumPushValidationOutcome,
    EnumSuiteVerdict,
    ModelPushValidationReceipt,
)
from omnimarket.nodes.node_push_validation_effect.models.model_push_validation_request import (
    EnumPushValidationMode,
    ModelBundleRef,
    ModelPushValidationRequest,
)
from omnimarket.nodes.node_push_validation_effect.protocols.git_push_validation_subprocess import (
    GitPushValidationSubprocess,
)
from omnimarket.nodes.node_push_validation_effect.protocols.protocol_push_validation_client import (
    ProtocolPushValidationClient,
)

logger = logging.getLogger(__name__)

_TENANT_PRINCIPAL_PATTERN = re.compile(r"^t-[0-9a-f]{32}$")
_PROTECTED_BRANCHES = frozenset({"dev", "main"})


def _utc_now_z() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


class HandlerPushValidationEffect:
    """EFFECT handler: hook-verified, suite-gated, fail-closed branch push.

    All side effects live behind the injected ``ProtocolPushValidationClient``
    (tests inject seeded stubs; runtime uses the subprocess-backed
    ``GitPushValidationSubprocess``). The handler owns only the outcome semantics.
    """

    def __init__(self, client: ProtocolPushValidationClient | None = None) -> None:
        self._client = client or GitPushValidationSubprocess()

    @property
    def handler_type(self) -> Literal["NODE_HANDLER"]:
        return "NODE_HANDLER"

    @property
    def handler_category(self) -> Literal["EFFECT"]:
        return "EFFECT"

    async def handle(
        self,
        request: ModelPushValidationRequest,
    ) -> ModelPushValidationReceipt:
        started_at = _utc_now_z()

        # Tenant gate BEFORE any side effect (optional-input-silent-skip is
        # banned). Raise -> failure terminal topic: a malformed principal
        # cannot key a tenant-scoped completed-topic receipt.
        principal = request.tenant_principal_id
        if not isinstance(principal, str) or not _TENANT_PRINCIPAL_PATTERN.fullmatch(
            principal
        ):
            raise ValueError(
                "tenant_principal_id absent or malformed (expected t-<32hex>): "
                f"{principal!r} — refusing; a receipt cannot be tenant-scoped "
                "to an invalid principal"
            )

        logger.info(
            "push_validation_effect: repo=%s branch=%s expected=%s correlation_id=%s",
            request.repo,
            request.branch,
            request.expected_head_sha,
            request.correlation_id,
        )
        return await asyncio.to_thread(self._run_sync, request, started_at)

    # -- sync flow (all client I/O) -----------------------------------------

    def _run_sync(
        self, request: ModelPushValidationRequest, started_at: str
    ) -> ModelPushValidationReceipt:
        host_identity = self._client.read_host_identity()
        credential_identity = self._client.read_credential_identity()

        def receipt(
            outcome: EnumPushValidationOutcome,
            *,
            hook_id_readback: str = "",
            suite_verdict: EnumSuiteVerdict = EnumSuiteVerdict.NOT_RUN,
            suite_log_digest: str | None = None,
            push_exit: int | None = None,
            remote_sha_readback: str | None = None,
            failure_detail: str | None = None,
        ) -> ModelPushValidationReceipt:
            return ModelPushValidationReceipt(
                outcome=outcome,
                correlation_id=request.correlation_id,
                tenant_principal_id=request.tenant_principal_id,
                tenant_id=request.tenant_id,
                requester=request.requester,
                repo=request.repo,
                branch=request.branch,
                expected_head_sha=request.expected_head_sha,
                hook_id_readback=hook_id_readback,
                suite_verdict=suite_verdict,
                suite_log_digest=suite_log_digest,
                push_exit=push_exit,
                remote_sha_readback=remote_sha_readback,
                host_identity=host_identity,
                credential_identity=credential_identity,
                failure_detail=failure_detail,
                started_at=started_at,
                completed_at=_utc_now_z(),
                mode=request.mode,
            )

        # (4) Protected branches are refused before any git side effect.
        if request.branch in _PROTECTED_BRANCHES:
            return receipt(
                EnumPushValidationOutcome.REFUSED,
                failure_detail="protected_branch_refused",
            )

        # (7) Bundle transfer leg (OMN-14979). A tree/commit+patch identity
        # names state that is NOT on origin, so the origin-state checks below
        # (already_pushed, stale_head) are meaningless for it — the state
        # under test is the bundle, not the remote branch. Materialize first,
        # then run the suite against the unpacked ref.
        #
        # The push path is unreachable here: the request model already
        # enforces mode=validate_only for every non-commit identity, so a
        # bundle can never reach push_branch. That invariant is re-asserted
        # below rather than assumed.
        bundle = getattr(request.source_identity, "bundle", None)
        if bundle is not None:
            # Tenant isolation re-check. The request model validates this too,
            # but model_construct bypasses validation entirely, so a
            # model-only check is not an authorization control (same reasoning
            # as the tenant_principal_id gate above).
            expected_prefix = ModelBundleRef.tenant_key_prefix(
                request.tenant_principal_id
            )
            if not bundle.key.startswith(expected_prefix):
                return receipt(
                    EnumPushValidationOutcome.REFUSED,
                    failure_detail=(
                        "cross_tenant_bundle_refused: bundle key is not under "
                        "this tenant's prefix"
                    ),
                )

            if request.mode != EnumPushValidationMode.VALIDATE_ONLY:
                raise RuntimeError(
                    "a bundle-backed source identity reached the handler with "
                    f"mode={request.mode.value} — the push flow must never "
                    "push an unpushed bundle; refusing to emit a receipt for "
                    "an impossible request shape"
                )

            materialization = self._client.materialize_bundle(
                request.repo,
                request.branch,
                bundle,
                request.correlation_id,
            )
            if not materialization.materialized:
                mode_value = (
                    materialization.failure_mode.value
                    if materialization.failure_mode is not None
                    else "unspecified"
                )
                return receipt(
                    EnumPushValidationOutcome.BUNDLE_UNAVAILABLE,
                    failure_detail=(
                        f"bundle_{mode_value}"
                        f"; observed_sha256={materialization.observed_sha256 or 'none'}"
                        f"; observed_size_bytes={materialization.observed_size_bytes}"
                        + (
                            f"; {materialization.detail}"
                            if materialization.detail
                            else ""
                        )
                    ),
                )

            hooks = self._client.install_hooks(request.repo, request.branch)
            if not hooks.installed or not hooks.hook_id_readback.strip():
                return receipt(
                    EnumPushValidationOutcome.REFUSED,
                    failure_detail=(
                        "hook_readback_failed_refusing_unhooked_push"
                        + (f": {hooks.detail}" if hooks.detail else "")
                    ),
                )

            # The suite MUST run against the materialized ref. Running the
            # origin commit here would make the whole leg theater.
            bundle_suite = self._client.run_suite(
                request.repo,
                request.branch,
                request.expected_head_sha,
                source_ref=materialization.materialized_ref,
            )
            if not bundle_suite.log_digest.strip():
                raise RuntimeError(
                    "governed suite returned an empty log digest — a run "
                    "suite always has a complete log digest; refusing to emit "
                    "an unverifiable receipt"
                )
            if not bundle_suite.passed:
                return receipt(
                    EnumPushValidationOutcome.SUITE_FAILED,
                    hook_id_readback=hooks.hook_id_readback,
                    suite_verdict=EnumSuiteVerdict.FAIL,
                    suite_log_digest=bundle_suite.log_digest,
                    failure_detail=bundle_suite.detail or "governed suite red",
                )
            return receipt(
                EnumPushValidationOutcome.VALIDATED,
                hook_id_readback=hooks.hook_id_readback,
                suite_verdict=EnumSuiteVerdict.PASS,
                suite_log_digest=bundle_suite.log_digest,
            )

        # Exactly ONE observation — never refetch-and-continue.
        observation = self._client.observe_branch(
            request.repo, request.branch, request.expected_head_sha
        )

        # (5) Idempotent redelivery FIRST: the destination already has the
        # target as pushed state -> never double-push.
        if observation.remote_contains_expected:
            return receipt(
                EnumPushValidationOutcome.ALREADY_PUSHED,
                remote_sha_readback=observation.remote_head_sha,
            )

        # (1) FAIL-CLOSED stale head: abort, record the observed divergent head.
        if observation.observed_head_sha != request.expected_head_sha:
            return receipt(
                EnumPushValidationOutcome.STALE_HEAD,
                remote_sha_readback=observation.observed_head_sha,
                failure_detail="stale_expected_head_sha",
            )

        # (3) Install hooks + capture hook_id_readback BEFORE any push; a
        # failed readback refuses — never proceed unhooked.
        hooks = self._client.install_hooks(request.repo, request.branch)
        if not hooks.installed or not hooks.hook_id_readback.strip():
            return receipt(
                EnumPushValidationOutcome.REFUSED,
                failure_detail=(
                    "hook_readback_failed_refusing_unhooked_push"
                    + (f": {hooks.detail}" if hooks.detail else "")
                ),
            )

        # (2) Governed suite; red NEVER pushes.
        suite = self._client.run_suite(
            request.repo, request.branch, request.expected_head_sha
        )
        if not suite.log_digest.strip():
            raise RuntimeError(
                "governed suite returned an empty log digest — a run suite "
                "always has a complete log digest; refusing to emit an "
                "unverifiable receipt"
            )
        if not suite.passed:
            return receipt(
                EnumPushValidationOutcome.SUITE_FAILED,
                hook_id_readback=hooks.hook_id_readback,
                suite_verdict=EnumSuiteVerdict.FAIL,
                suite_log_digest=suite.log_digest,
                failure_detail=suite.detail or "governed suite red",
            )

        # (6) Contract v2 (OMN-14976): validate_only stops here — suite pass,
        # push intentionally never attempted. push_branch is not called at
        # all (not merely skipped-and-recorded): validate_only must never
        # touch the remote.
        if request.mode == EnumPushValidationMode.VALIDATE_ONLY:
            return receipt(
                EnumPushValidationOutcome.VALIDATED,
                hook_id_readback=hooks.hook_id_readback,
                suite_verdict=EnumSuiteVerdict.PASS,
                suite_log_digest=suite.log_digest,
            )

        push = self._client.push_branch(
            request.repo, request.branch, request.expected_head_sha
        )
        if push.exit_code != 0:
            return receipt(
                EnumPushValidationOutcome.PUSH_FAILED,
                hook_id_readback=hooks.hook_id_readback,
                suite_verdict=EnumSuiteVerdict.PASS,
                suite_log_digest=suite.log_digest,
                push_exit=push.exit_code,
                remote_sha_readback=push.remote_sha_readback,
                failure_detail=push.detail or "git push failed",
            )

        if push.remote_sha_readback != request.expected_head_sha:
            # Integrity anomaly: push exited 0 but the post-push readback does
            # not show the validated SHA (concurrent writer / readback
            # failure). The receipt binds outcome to the pushed SHA, so this
            # cannot honestly be outcome=pushed -> failure terminal topic.
            raise RuntimeError(
                "push exited 0 but post-push remote readback is "
                f"{push.remote_sha_readback!r}, not the validated "
                f"{request.expected_head_sha!r} — refusing to emit a pushed "
                "receipt that does not bind to the pushed SHA"
            )

        return receipt(
            EnumPushValidationOutcome.PUSHED,
            hook_id_readback=hooks.hook_id_readback,
            suite_verdict=EnumSuiteVerdict.PASS,
            suite_log_digest=suite.log_digest,
            push_exit=0,
            remote_sha_readback=push.remote_sha_readback,
        )


__all__ = ["HandlerPushValidationEffect"]
