# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Real conflict adapter: structural conflict detection (OMN-14529).

Reuses the pure ``structural_confidence()`` function already implemented and
tested in ``omnibase_infra`` (Stage 2 of ``HandlerWriteDecision``) rather than
re-deriving it — this adapter is the pre-persist mirror of that same
structural check, used to decide whether the orchestrator needs to run
semantic review / trigger the Slack notify gate BEFORE writing.

Queries existing decisions directly via
``PostgresDecisionStore.query_active_decisions_raw`` (not the
Protocol-facing ``query_decisions``) specifically to retain the real
``decision_id`` from the database — the orchestrator's own
``ModelDecisionEntry`` has no id field, so the Protocol-facing query path
loses decision identity on the way back.

Severity matrix and modifiers are the ones documented in
``decision_store``'s SKILL.md (base severity by decision_type x layer, then
platform-wide-scope and architecture-layer floors). The third documented
modifier — a semantic-review shift — never applies here because semantic
review is deferred (see ``deferred_semantic_review.py``).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any
from uuid import uuid4

from omnimarket.nodes.node_decision_store_orchestrator.adapters.postgres_decision_store import (
    PostgresDecisionStore,
)
from omnimarket.nodes.node_decision_store_orchestrator.models.model_decision_store_request import (
    EnumConflictSeverity,
    EnumDecisionLayer,
    EnumDecisionType,
    ModelDecisionEntry,
)

# Base severity matrix from decision_store's SKILL.md ("Severity Matrix").
_BASE_SEVERITY: dict[
    tuple[EnumDecisionType, EnumDecisionLayer], EnumConflictSeverity
] = {
    (
        EnumDecisionType.TECH_STACK_CHOICE,
        EnumDecisionLayer.ARCHITECTURE,
    ): EnumConflictSeverity.HIGH,
    (
        EnumDecisionType.TECH_STACK_CHOICE,
        EnumDecisionLayer.DESIGN,
    ): EnumConflictSeverity.HIGH,
    (
        EnumDecisionType.TECH_STACK_CHOICE,
        EnumDecisionLayer.PLANNING,
    ): EnumConflictSeverity.MEDIUM,
    (
        EnumDecisionType.DESIGN_PATTERN,
        EnumDecisionLayer.ARCHITECTURE,
    ): EnumConflictSeverity.HIGH,
    (
        EnumDecisionType.DESIGN_PATTERN,
        EnumDecisionLayer.DESIGN,
    ): EnumConflictSeverity.MEDIUM,
    (
        EnumDecisionType.DESIGN_PATTERN,
        EnumDecisionLayer.PLANNING,
    ): EnumConflictSeverity.LOW,
    (
        EnumDecisionType.API_CONTRACT,
        EnumDecisionLayer.ARCHITECTURE,
    ): EnumConflictSeverity.HIGH,
    (
        EnumDecisionType.API_CONTRACT,
        EnumDecisionLayer.DESIGN,
    ): EnumConflictSeverity.MEDIUM,
    (
        EnumDecisionType.API_CONTRACT,
        EnumDecisionLayer.PLANNING,
    ): EnumConflictSeverity.LOW,
    (
        EnumDecisionType.SCOPE_BOUNDARY,
        EnumDecisionLayer.ARCHITECTURE,
    ): EnumConflictSeverity.MEDIUM,
    (
        EnumDecisionType.SCOPE_BOUNDARY,
        EnumDecisionLayer.DESIGN,
    ): EnumConflictSeverity.MEDIUM,
    (
        EnumDecisionType.SCOPE_BOUNDARY,
        EnumDecisionLayer.PLANNING,
    ): EnumConflictSeverity.LOW,
    (
        EnumDecisionType.REQUIREMENT_CHOICE,
        EnumDecisionLayer.ARCHITECTURE,
    ): EnumConflictSeverity.MEDIUM,
    (
        EnumDecisionType.REQUIREMENT_CHOICE,
        EnumDecisionLayer.DESIGN,
    ): EnumConflictSeverity.LOW,
    (
        EnumDecisionType.REQUIREMENT_CHOICE,
        EnumDecisionLayer.PLANNING,
    ): EnumConflictSeverity.LOW,
}

_SEVERITY_ORDER = (
    EnumConflictSeverity.LOW,
    EnumConflictSeverity.MEDIUM,
    EnumConflictSeverity.HIGH,
)


def _floor(
    severity: EnumConflictSeverity, minimum: EnumConflictSeverity
) -> EnumConflictSeverity:
    if _SEVERITY_ORDER.index(severity) < _SEVERITY_ORDER.index(minimum):
        return minimum
    return severity


def structural_confidence(
    domain_a: str,
    layer_a: str,
    services_a: tuple[str, ...],
    domain_b: str,
    layer_b: str,
    services_b: tuple[str, ...],
) -> float:
    """Structural conflict confidence between two decisions.

    Identical semantics to ``omnibase_infra``'s Stage 2 pure function
    (``handler_write_decision.structural_confidence``), reimplemented at the
    orchestrator's field granularity rather than importing across the
    infra/omnimarket boundary for a pure, dependency-free function — see
    ``ALLOWED_DOMAINS``/case normalization notes there for the source.
    """
    if domain_a.lower() != domain_b.lower():
        return 0.0
    if layer_a != layer_b:
        return 0.4
    svc_a = {s.lower() for s in services_a}
    svc_b = {s.lower() for s in services_b}
    if not svc_a and not svc_b:
        return 0.9
    if not svc_a or not svc_b:
        return 0.8
    if svc_a == svc_b:
        return 1.0
    if svc_a & svc_b:
        return 0.7
    return 0.3


def compute_severity(
    decision_type: EnumDecisionType,
    layer: EnumDecisionLayer,
    other_layer: EnumDecisionLayer,
    services: tuple[str, ...],
    other_services: tuple[str, ...],
) -> EnumConflictSeverity:
    """Apply the SKILL.md severity matrix + platform-wide/architecture floors."""
    severity = _BASE_SEVERITY[(decision_type, layer)]
    if not services or not other_services:
        severity = _floor(severity, EnumConflictSeverity.MEDIUM)
    if (
        layer == EnumDecisionLayer.ARCHITECTURE
        or other_layer == EnumDecisionLayer.ARCHITECTURE
    ):
        severity = _floor(severity, EnumConflictSeverity.HIGH)
    return severity


class StructuralConflictCheck:
    """Real ``ProtocolDecisionConflictAdapter`` implementation."""

    def __init__(self, store_adapter: PostgresDecisionStore | None = None) -> None:
        self._store_adapter = store_adapter or PostgresDecisionStore()

    def check_conflicts(
        self, entry: ModelDecisionEntry, *, scope: str
    ) -> list[Mapping[str, Any]]:
        domain = scope or entry.domain
        existing = self._store_adapter.query_active_decisions_raw(
            domain=domain, layer=entry.layer.value
        )
        conflicts: list[Mapping[str, Any]] = []
        for other in existing:
            other_layer_raw = other.scope_layer
            try:
                other_layer = EnumDecisionLayer(other_layer_raw)
            except ValueError:
                # decision_store's scope_layer also admits "implementation",
                # which the orchestrator's EnumDecisionLayer does not model.
                # Skip rather than crash the whole conflict check.
                continue
            confidence = structural_confidence(
                entry.domain,
                entry.layer.value,
                entry.services,
                other.scope_domain,
                other.scope_layer,
                tuple(other.scope_services),
            )
            if confidence <= 0.0:
                continue
            severity = compute_severity(
                entry.decision_type,
                entry.layer,
                other_layer,
                entry.services,
                tuple(other.scope_services),
            )
            conflicts.append(
                {
                    "conflict_id": str(uuid4()),
                    # The entry being checked has no id yet (not persisted) —
                    # ModelDecisionEntry carries no identity field at all.
                    "entry_a_id": "pending",
                    "entry_b_id": str(other.decision_id),
                    "structural_confidence": confidence,
                    "severity": severity,
                    "status": "OPEN",
                    "semantic_checked": False,
                }
            )
        return conflicts


__all__ = [
    "StructuralConflictCheck",
    "compute_severity",
    "structural_confidence",
]
