# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Pure TC.5 reconciliation of admission evidence against registry state."""

from __future__ import annotations

import hashlib
import json

from omnimarket.models.exemplar_admission import (
    ModelAdmissionDecision,
)
from omnimarket.nodes.node_exemplar_registry_reconcile_compute.models.model_exemplar_registry_reconcile import (
    ModelExemplarEvaluationCounts,
    ModelExemplarRegistryOperation,
    ModelExemplarRegistryReconcileRequest,
    ModelExemplarRegistryReconcileResult,
    ModelExemplarRegistryRow,
    RegistryLifecycle,
    RegistryOperation,
)


def _hash(value: dict[str, object]) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _version_digest(
    request: ModelExemplarRegistryReconcileRequest,
    logical_source_id: str,
    decision: ModelAdmissionDecision,
) -> str:
    if decision.node_id not in request.content_sha256:
        raise ValueError(f"Missing content_sha256 for {decision.node_id}")
    return _hash(
        {
            "logical_source_id": logical_source_id,
            "node_archetype": decision.node_archetype,
            "contract_path": decision.contract_path,
            "handler_module": decision.handler_module,
            "content_sha256": request.content_sha256[decision.node_id],
            "gate": request.admission.gate,
            "gate_version": request.admission.gate_version,
            "rules_version": request.admission.rules_version,
        }
    )


def _operation(
    request: ModelExemplarRegistryReconcileRequest,
    logical_source_id: str,
    current: ModelExemplarRegistryRow | None,
    decision: ModelAdmissionDecision | None,
) -> ModelExemplarRegistryOperation | None:
    admitted = decision is not None and decision.admitted
    reason_codes: list[str] = (
        ["absent_at_commit"]
        if decision is None
        else []
        if decision.admitted
        else [violation.code for violation in decision.violations]
    )
    new_digest = (
        _version_digest(request, logical_source_id, decision)
        if decision is not None and admitted
        else None
    )
    operation: RegistryOperation
    lifecycle_after: RegistryLifecycle
    if current is None:
        if decision is None or not admitted:
            return None
        node_id = decision.node_id
        prior_digest = None
        version_digest = new_digest
        operation, lifecycle_after = "create", "active"
    else:
        node_id = current.node_id
        prior_digest = current.version_digest
        version_digest = current.version_digest
        operation, lifecycle_after = "retain", current.lifecycle
        evaluation = request.evaluation_counts.get(
            node_id, ModelExemplarEvaluationCounts()
        )
        enough_samples = evaluation.sample_size >= request.policy.minimum_sample_size
        quality_reasons = []
        if (
            evaluation.true_positive + evaluation.false_positive
            >= request.policy.minimum_sample_size
            and evaluation.precision is not None
            and evaluation.precision < request.policy.minimum_precision
        ):
            quality_reasons.append("precision_below_threshold")
        if (
            evaluation.true_positive + evaluation.false_negative
            >= request.policy.minimum_sample_size
            and evaluation.recall is not None
            and evaluation.recall < request.policy.minimum_recall
        ):
            quality_reasons.append("recall_below_threshold")
        if admitted:
            if (
                current.lifecycle == "deprecated"
                or new_digest != current.version_digest
            ):
                operation, lifecycle_after = "revise", "active"
                version_digest = new_digest
        elif current.lifecycle != "revoked":
            if enough_samples and any(
                code in request.policy.revoke_on_codes for code in reason_codes
            ):
                operation, lifecycle_after = "revoke", "revoked"
            elif current.lifecycle == "active":
                operation, lifecycle_after = "deprecate", "deprecated"
        if current.lifecycle != "revoked":
            if enough_samples and quality_reasons:
                operation, lifecycle_after = "revoke", "revoked"
                reason_codes.extend(quality_reasons)
            elif not enough_samples and any(
                code in request.policy.revoke_on_codes for code in reason_codes
            ):
                reason_codes.append("insufficient_evaluation_sample")

    return ModelExemplarRegistryOperation(
        operation_id=_hash(
            {
                "repository": request.repository,
                "commit": request.admission.commit,
                "logical_source_id": logical_source_id,
                "operation": operation,
                "version_digest": version_digest,
                "lifecycle_after": lifecycle_after,
                "reason_codes": reason_codes,
                "policy": request.policy.model_dump(mode="json"),
                "evaluation": request.evaluation_counts.get(
                    node_id, ModelExemplarEvaluationCounts()
                ).model_dump(mode="json"),
            }
        ),
        logical_source_id=logical_source_id,
        node_id=node_id,
        operation=operation,
        lifecycle_after=lifecycle_after,
        prior_version_digest=prior_digest,
        version_digest=version_digest,
        reason_codes=reason_codes,
        evaluation=request.evaluation_counts.get(
            node_id, ModelExemplarEvaluationCounts()
        ),
        receipt=decision.receipt
        if decision is not None and admitted and lifecycle_after == "active"
        else None,
    )


class HandlerExemplarRegistryReconcile:
    """Plan one operation per admitted or currently registered logical source."""

    def handle(
        self, request: ModelExemplarRegistryReconcileRequest
    ) -> ModelExemplarRegistryReconcileResult:
        if request.admission.repository != request.repository:
            raise ValueError(
                f"Admission repository mismatch: {request.admission.repository}"
            )
        current_rows: dict[str, ModelExemplarRegistryRow] = {}
        for row in request.current_rows:
            if not row.logical_source_id.startswith(f"{request.repository}/"):
                raise ValueError(
                    f"Registry row repository mismatch: {row.logical_source_id}"
                )
            if row.logical_source_id in current_rows:
                raise ValueError(f"duplicate current row: {row.logical_source_id}")
            current_rows[row.logical_source_id] = row

        decisions: dict[str, ModelAdmissionDecision] = {}
        for decision in request.admission.decisions:
            logical_source_id = f"{request.repository}/{decision.node_id}"
            if logical_source_id in decisions:
                raise ValueError(f"duplicate admission decision: {decision.node_id}")
            decisions[logical_source_id] = decision
        operations: list[ModelExemplarRegistryOperation] = []
        counts = dict.fromkeys(("create", "revise", "retain", "deprecate", "revoke"), 0)
        for logical_source_id in sorted(current_rows.keys() | decisions.keys()):
            operation = _operation(
                request,
                logical_source_id,
                current_rows.get(logical_source_id),
                decisions.get(logical_source_id),
            )
            if operation is not None:
                operations.append(operation)
                counts[operation.operation] += 1

        return ModelExemplarRegistryReconcileResult(
            repository=request.repository,
            commit=request.admission.commit,
            policy_version=request.policy.policy_version,
            policy=request.policy,
            operations=operations,
            counts=counts,
            current_count=len(request.current_rows),
            admitted_count=request.admission.admitted_count,
            reconciliation_hash=_hash(
                {
                    "repository": request.repository,
                    "commit": request.admission.commit,
                    "policy_version": request.policy.policy_version,
                    "policy": request.policy.model_dump(mode="json"),
                    "operations": [op.model_dump(mode="json") for op in operations],
                }
            ),
        )
