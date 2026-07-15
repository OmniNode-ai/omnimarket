# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Handler for node_decision_store_orchestrator [OMN-12219].

The orchestrator owns record/query/check-conflicts sequencing. Structural
checks, semantic review, persistence, and notification are injected native
adapters so the handler does not import other node handlers or call external
services directly.

OMN-14529: the four adapter constructor args defaulted to ``None``, which
meant the generic ``onex skill`` / receipt-mode dispatch — which instantiates
the contract-declared handler class with zero arguments, the same way
``node_dod_sweep_orchestrator`` does — always hit
``RuntimeError("... adapter required ...")`` on any live ``record`` call.
There was no dispatch route (the original gap) AND, even once wired, the
route would have failed closed instantly. The defaults below follow the
``node_dod_sweep_orchestrator`` pattern: real, working, module-level default
collaborators instead of ``None`` sentinels. See
``adapters/__init__.py`` for what "real" means for each of the four.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Protocol

from omnimarket.nodes.node_decision_store_orchestrator.adapters import (
    DeferredSemanticReview,
    LogOnlyNotify,
    PostgresDecisionStore,
    StructuralConflictCheck,
)
from omnimarket.nodes.node_decision_store_orchestrator.models.model_decision_store_request import (
    EnumConflictSeverity,
    EnumDecisionAction,
    ModelConflictResult,
    ModelDecisionEntry,
    ModelDecisionQueryFilter,
    ModelDecisionStoreRequest,
    ModelDecisionStoreResult,
)


class ProtocolDecisionConflictAdapter(Protocol):
    """Adapter boundary for structural conflict checks."""

    def check_conflicts(
        self, entry: ModelDecisionEntry, *, scope: str
    ) -> list[Mapping[str, Any] | ModelConflictResult]: ...


class ProtocolDecisionSemanticReviewAdapter(Protocol):
    """Adapter boundary for semantic LLM conflict review."""

    def review_conflicts(
        self,
        entry: ModelDecisionEntry,
        conflicts: tuple[ModelConflictResult, ...],
    ) -> list[Mapping[str, Any] | ModelConflictResult]: ...


class ProtocolDecisionStoreAdapter(Protocol):
    """Adapter boundary for durable decision-store reads and writes."""

    def persist_decision(
        self,
        entry: ModelDecisionEntry,
        conflicts: tuple[ModelConflictResult, ...],
    ) -> str: ...

    def query_decisions(
        self, query_filter: ModelDecisionQueryFilter | None
    ) -> Mapping[str, Any]: ...


class ProtocolDecisionNotifyAdapter(Protocol):
    """Adapter boundary for high-severity conflict notification gates."""

    def notify_high_conflicts(
        self,
        entry: ModelDecisionEntry,
        conflicts: tuple[ModelConflictResult, ...],
    ) -> None: ...


# Shared, stateless default collaborators — constructed once at import time
# and reused across every bare HandlerDecisionStoreOrchestrator() the generic
# dispatch creates, mirroring node_dod_sweep_orchestrator's real-default-fn
# pattern. Each adapter resolves its DSN lazily per-call (see
# postgres_decision_store.py), so importing this module never
# requires OMNIBASE_INFRA_DB_URL to be set.
_DEFAULT_STORE_ADAPTER = PostgresDecisionStore()
_DEFAULT_CONFLICT_ADAPTER = StructuralConflictCheck(_DEFAULT_STORE_ADAPTER)
_DEFAULT_SEMANTIC_ADAPTER = DeferredSemanticReview()
_DEFAULT_NOTIFY_ADAPTER = LogOnlyNotify()


class HandlerDecisionStoreOrchestrator:
    """Route decision-store actions through native adapter boundaries."""

    def __init__(
        self,
        conflict_adapter: ProtocolDecisionConflictAdapter | None = None,
        semantic_adapter: ProtocolDecisionSemanticReviewAdapter | None = None,
        store_adapter: ProtocolDecisionStoreAdapter | None = None,
        notify_adapter: ProtocolDecisionNotifyAdapter | None = None,
    ) -> None:
        self._conflict_adapter = conflict_adapter or _DEFAULT_CONFLICT_ADAPTER
        self._semantic_adapter = semantic_adapter or _DEFAULT_SEMANTIC_ADAPTER
        self._store_adapter = store_adapter or _DEFAULT_STORE_ADAPTER
        self._notify_adapter = notify_adapter or _DEFAULT_NOTIFY_ADAPTER

    def handle(self, request: ModelDecisionStoreRequest) -> ModelDecisionStoreResult:
        if request.action == EnumDecisionAction.QUERY:
            return self._handle_query(request)
        if request.action == EnumDecisionAction.CHECK_CONFLICTS:
            return self._handle_check_conflicts(request)
        return self._handle_record(request)

    def _handle_query(
        self, request: ModelDecisionStoreRequest
    ) -> ModelDecisionStoreResult:
        if self._store_adapter is None:
            raise RuntimeError("store adapter required for decision query")
        raw = self._store_adapter.query_decisions(request.query_filter)
        entries = tuple(_entry(item) for item in raw.get("entries", ()) or ())
        next_cursor = raw.get("next_cursor")
        return ModelDecisionStoreResult(
            action=request.action,
            query_results=entries,
            next_cursor=str(next_cursor) if next_cursor else None,
            dry_run=request.dry_run,
        )

    def _handle_check_conflicts(
        self, request: ModelDecisionStoreRequest
    ) -> ModelDecisionStoreResult:
        entry = _required_entry(request)
        conflicts = self._structural_conflicts(request, entry)
        return _result_for_conflicts(
            action=request.action,
            conflicts=conflicts,
            dry_run=request.dry_run,
            slack_gate_triggered=False,
        )

    def _handle_record(
        self, request: ModelDecisionStoreRequest
    ) -> ModelDecisionStoreResult:
        entry = _required_entry(request)
        conflicts = self._structural_conflicts(request, entry)
        if _needs_semantic_review(conflicts) and not request.dry_run:
            if self._semantic_adapter is None:
                raise RuntimeError(
                    "semantic adapter required for high-confidence conflicts"
                )
            conflicts = tuple(
                _conflict(item)
                for item in self._semantic_adapter.review_conflicts(entry, conflicts)
            )

        stored_decision_id = None
        if not request.dry_run:
            if self._store_adapter is None:
                raise RuntimeError("store adapter required when dry_run is false")
            stored_decision_id = self._store_adapter.persist_decision(entry, conflicts)

        high_conflicts = tuple(
            conflict
            for conflict in conflicts
            if conflict.severity == EnumConflictSeverity.HIGH
        )
        slack_gate_triggered = False
        if high_conflicts and not request.dry_run:
            if self._notify_adapter is None:
                raise RuntimeError(
                    "notify adapter required for high-severity conflicts"
                )
            self._notify_adapter.notify_high_conflicts(entry, high_conflicts)
            slack_gate_triggered = True

        return _result_for_conflicts(
            action=request.action,
            conflicts=conflicts,
            dry_run=request.dry_run,
            stored_decision_id=stored_decision_id,
            slack_gate_triggered=slack_gate_triggered,
        )

    def _structural_conflicts(
        self, request: ModelDecisionStoreRequest, entry: ModelDecisionEntry
    ) -> tuple[ModelConflictResult, ...]:
        if self._conflict_adapter is None:
            raise RuntimeError("conflict adapter required for decision conflict check")
        scope = request.conflict_scope or entry.domain
        return tuple(
            _conflict(item)
            for item in self._conflict_adapter.check_conflicts(entry, scope=scope)
        )


def _required_entry(request: ModelDecisionStoreRequest) -> ModelDecisionEntry:
    if request.entry is None:
        raise ValueError(f"entry is required for {request.action.value}")
    return request.entry


def _needs_semantic_review(conflicts: tuple[ModelConflictResult, ...]) -> bool:
    return any(conflict.structural_confidence >= 0.6 for conflict in conflicts)


def _conflict(raw: Mapping[str, Any] | ModelConflictResult) -> ModelConflictResult:
    if isinstance(raw, ModelConflictResult):
        return raw
    return ModelConflictResult.model_validate(raw)


def _entry(raw: Mapping[str, Any] | ModelDecisionEntry) -> ModelDecisionEntry:
    if isinstance(raw, ModelDecisionEntry):
        return raw
    return ModelDecisionEntry.model_validate(raw)


def _result_for_conflicts(
    *,
    action: EnumDecisionAction,
    conflicts: tuple[ModelConflictResult, ...],
    dry_run: bool,
    stored_decision_id: str | None = None,
    slack_gate_triggered: bool,
) -> ModelDecisionStoreResult:
    return ModelDecisionStoreResult(
        action=action,
        stored_decision_id=stored_decision_id,
        conflicts_found=conflicts,
        high_severity_count=sum(
            1
            for conflict in conflicts
            if conflict.severity == EnumConflictSeverity.HIGH
        ),
        slack_gate_triggered=slack_gate_triggered,
        dry_run=dry_run,
    )
