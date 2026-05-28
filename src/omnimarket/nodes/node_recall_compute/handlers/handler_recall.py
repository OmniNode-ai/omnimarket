"""Handler for node_recall_compute.

Native recall compute owns backend selection, deduplication, ranking, and
response shaping. Retrieval itself is supplied by injected Onex-native backend
adapters; this handler does not call the legacy localhost HTTP API.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Protocol

from omnimarket.nodes.node_recall_compute.models.model_recall_request import (
    RECALL_SCOPES,
    ModelRecallFilters,
    ModelRecallRequest,
)
from omnimarket.nodes.node_recall_compute.models.model_recall_result import (
    EnumRecallConfidence,
    ModelKnowledgeResult,
    ModelRecallResult,
)

_BACKEND_SCOPES = ("learnings", "architecture", "antipatterns")


class ProtocolRecallBackend(Protocol):
    """Adapter boundary for a native recall backend."""

    def query(
        self,
        query: str,
        *,
        filters: ModelRecallFilters | None,
        max_results: int,
    ) -> list[Mapping[str, Any] | ModelKnowledgeResult]: ...


class NodeRecallCompute:
    """Federate recall requests across injected native knowledge backends."""

    def __init__(
        self, backends: Mapping[str, ProtocolRecallBackend] | None = None
    ) -> None:
        self._backends = dict(backends or {})

    def handle(self, request: ModelRecallRequest) -> ModelRecallResult:
        selected = _selected_backends(request.scope)
        if not self._backends:
            return ModelRecallResult(
                partial=True,
                error="no recall backends configured",
            )

        results: list[ModelKnowledgeResult] = []
        failed: list[str] = []
        missing: list[str] = []
        for backend_name in selected:
            backend = self._backends.get(backend_name)
            if backend is None:
                missing.append(backend_name)
                continue
            try:
                raw_results = backend.query(
                    request.query,
                    filters=request.filters,
                    max_results=request.max_results,
                )
            except Exception as exc:  # pragma: no cover - backend-specific failure
                failed.append(f"{backend_name}: {exc}")
                continue
            results.extend(_coerce_result(backend_name, item) for item in raw_results)

        ranked = _rank_results(_dedupe_results(results), request.max_results)
        partial = bool(missing or failed)
        error = _error(missing, failed)
        return ModelRecallResult(
            results=tuple(ranked),
            sources=tuple(sorted({result.source for result in ranked})),
            confidence=_confidence(ranked),
            partial=partial,
            error=error,
        )


def _selected_backends(scope: str) -> tuple[str, ...]:
    if scope == "all":
        return _BACKEND_SCOPES
    if scope not in RECALL_SCOPES:
        raise ValueError("scope must be one of " + ", ".join(RECALL_SCOPES))
    return (scope,)


def _coerce_result(
    backend_name: str, raw: Mapping[str, Any] | ModelKnowledgeResult
) -> ModelKnowledgeResult:
    if isinstance(raw, ModelKnowledgeResult):
        if raw.source:
            return raw
        return raw.model_copy(update={"source": backend_name})
    payload = dict(raw)
    payload.setdefault("source", backend_name)
    return ModelKnowledgeResult.model_validate(payload)


def _dedupe_results(results: list[ModelKnowledgeResult]) -> list[ModelKnowledgeResult]:
    by_key: dict[tuple[str, str], ModelKnowledgeResult] = {}
    for result in results:
        key = (result.source, " ".join(result.content.split()).lower())
        current = by_key.get(key)
        if current is None or (result.similarity or 0.0) > (current.similarity or 0.0):
            by_key[key] = result
    return list(by_key.values())


def _rank_results(
    results: list[ModelKnowledgeResult], max_results: int
) -> list[ModelKnowledgeResult]:
    ordered = sorted(
        results,
        key=lambda item: (
            -(item.similarity or 0.0),
            item.rank,
            item.source,
            item.content,
        ),
    )[:max_results]
    return [
        item.model_copy(update={"rank": index}) for index, item in enumerate(ordered)
    ]


def _confidence(results: list[ModelKnowledgeResult]) -> EnumRecallConfidence:
    if not results:
        return EnumRecallConfidence.NONE
    best = max(result.similarity or 0.0 for result in results)
    if best >= 0.8:
        return EnumRecallConfidence.HIGH
    if best >= 0.5:
        return EnumRecallConfidence.MEDIUM
    return EnumRecallConfidence.LOW


def _error(missing: list[str], failed: list[str]) -> str | None:
    parts: list[str] = []
    if missing:
        parts.append("missing backends: " + ", ".join(missing))
    if failed:
        parts.append("failed backends: " + "; ".join(failed))
    return "; ".join(parts) if parts else None


__all__ = ["NodeRecallCompute", "ProtocolRecallBackend"]
