# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerSuiteEvaluationEffect — fixed-commit suite-evaluation EFFECT
(OMN-16524, rung R1 of the Architecture-3 suite-execution ladder).

Canonical def-B handler: ``handle(request: ModelSuiteEvaluationRequest) ->
ModelSuiteEvaluationReceipt``. Root `CLAUDE.md` rule 7a: no `Plugin*` base
class, no envelope-in-core signature — the envelope boundary is the shared
runtime adapter; this module never references the event-envelope type.

EXTEND-vs-NET-NEW DECISION (OMN-16524's mandatory prior-art precheck,
recorded here per the ticket's own instruction to record it, not merely cite
the prior art and proceed):

`node_push_validation_effect` (OMN-14920) already ships a def-B EFFECT
handler that runs `uv run pytest` with fail-closed identity checking,
idempotent redelivery handling, and a working content-addressed bundle leg
(OMN-14979). This module EXTENDS that node's package — same contract, same
repo, same models package, same protocol-injection discipline, a SECOND
`handler_routing` operation (`run_suite_evaluation`) alongside the existing
`run_push_validation` — rather than a new node package.

What is NOT reused, and why: `HandlerPushValidationEffect`'s three
fail-closed checks (protected-branch refusal, `expected_head_sha`-vs-branch-
HEAD stale check via `observe_branch`, `already_pushed` idempotency) are
push-SAFETY controls — they exist to make sure the worker never pushes the
wrong commit to a live branch. This rung never pushes anything: it evaluates
one caller-named, already-merged, historical commit for its own sake. Reusing
`run_push_validation` as-is would force every R1 request to name a
non-protected branch whose live tip happens to equal the fixed commit (a
contortion that breaks the moment the branch advances) or would require
weakening a hardened security invariant that other, real push traffic still
depends on — both worse than a second operation with its own, narrower
request/receipt shape and its OWN protocol
(`ProtocolSuiteEvaluationClient`, deliberately smaller than
`ProtocolPushValidationClient`: no `observe_branch`, no `install_hooks`, no
`push_branch` — this operation touches none of those side effects).

What IS reused: `ModelSuiteRun` (protocol_push_validation_client.py) as the
pass/fail/log-digest shape, the tenant-gate pattern (identical regex check,
same "raise -> failure topic" discipline), the `_utc_now_z()` timestamp
convention, and the def-B/no-bus-access handler shape itself.

No idempotency claim store for R1: unlike push-validation (where a
redelivered command must not double-push a live branch), this operation has
no write side effect to protect against a duplicate — re-evaluating the same
commit twice produces two receipts with the same `evaluated_tree_digest` and
(baring suite flakiness) the same verdict. §8.4.1's idempotency discipline
is explicitly named in the plan as carrying over "in full" for the
architecture generally, but R1's own footprint note (§0.1e) does not require
it for THIS rung, and building a claim/dedup store for a read-only,
non-mutating operation is deferred rather than invented defensively here.
"""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import UTC, datetime
from typing import Literal

from omnimarket.nodes.node_push_validation_effect.models.model_suite_evaluation_receipt import (
    EnumSuiteEvaluationVerdict,
    ModelSuiteEvaluationReceipt,
)
from omnimarket.nodes.node_push_validation_effect.models.model_suite_evaluation_request import (
    ModelSuiteEvaluationRequest,
)
from omnimarket.nodes.node_push_validation_effect.protocols.gate_runner_suite_evaluation_subprocess import (
    SuiteEvaluationGateContainerSubprocess,
)
from omnimarket.nodes.node_push_validation_effect.protocols.protocol_suite_evaluation_client import (
    ProtocolSuiteEvaluationClient,
)

logger = logging.getLogger(__name__)

_TENANT_PRINCIPAL_PATTERN = re.compile(r"^t-[0-9a-f]{32}$")


def _utc_now_z() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


class HandlerSuiteEvaluationEffect:
    """EFFECT handler: fixed-commit suite evaluation, bounded execution.

    All side effects live behind the injected
    ``ProtocolSuiteEvaluationClient`` (tests inject seeded stubs; runtime
    uses the `docker exec`-backed ``SuiteEvaluationGateContainerSubprocess``).
    """

    def __init__(self, client: ProtocolSuiteEvaluationClient | None = None) -> None:
        self._client = client or SuiteEvaluationGateContainerSubprocess()

    @property
    def handler_type(self) -> Literal["NODE_HANDLER"]:
        return "NODE_HANDLER"

    @property
    def handler_category(self) -> Literal["EFFECT"]:
        return "EFFECT"

    async def handle(
        self,
        request: ModelSuiteEvaluationRequest,
    ) -> ModelSuiteEvaluationReceipt:
        started_at = _utc_now_z()

        # Tenant gate BEFORE any side effect (optional-input-silent-skip is
        # banned; model_construct bypass re-check, same discipline as the
        # sibling push-validation handler).
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
            "suite_evaluation_effect: repo=%s commit_sha=%s suite_scope=%s "
            "correlation_id=%s",
            request.repo,
            request.commit_sha,
            request.suite_scope,
            request.correlation_id,
        )
        return await asyncio.to_thread(self._run_sync, request, started_at)

    # -- sync flow (all client I/O) -----------------------------------------

    def _run_sync(
        self, request: ModelSuiteEvaluationRequest, started_at: str
    ) -> ModelSuiteEvaluationReceipt:
        host_identity = self._client.read_host_identity()

        # Exactly ONE observation call — checkout, tree digest, policy
        # digest, and suite run all happen inside this single client
        # boundary (mirrors "exactly one observation, never
        # refetch-and-continue" from the sibling handler).
        result = self._client.evaluate_commit(
            request.repo, request.commit_sha, request.suite_scope
        )

        if not result.suite.log_digest.strip():
            raise RuntimeError(
                "governed suite returned an empty log digest — a run suite "
                "always has a complete log digest; refusing to emit an "
                "unverifiable receipt"
            )

        verdict = (
            EnumSuiteEvaluationVerdict.PASS
            if result.suite.passed
            else EnumSuiteEvaluationVerdict.FAIL
        )

        return ModelSuiteEvaluationReceipt(
            correlation_id=request.correlation_id,
            tenant_principal_id=request.tenant_principal_id,
            requester=request.requester,
            repo=request.repo,
            commit_sha=request.commit_sha,
            evaluated_tree_digest=result.evaluated_tree_digest,
            selector_policy_digest=result.selector_policy_digest,
            suite_scope=request.suite_scope,
            verdict=verdict,
            suite_log_digest=result.suite.log_digest,
            host_identity=host_identity,
            failure_detail=(
                None
                if result.suite.passed
                else (result.suite.detail or "governed suite red")
            ),
            started_at=started_at,
            completed_at=_utc_now_z(),
        )


__all__ = ["HandlerSuiteEvaluationEffect"]
