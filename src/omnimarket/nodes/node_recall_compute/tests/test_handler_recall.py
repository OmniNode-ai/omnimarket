"""Tests for NodeRecallCompute native backend federation."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from omnimarket.nodes.node_recall_compute.handlers.handler_recall import (
    NodeRecallCompute,
)
from omnimarket.nodes.node_recall_compute.models.model_recall_request import (
    ModelRecallFilters,
    ModelRecallRequest,
)
from omnimarket.nodes.node_recall_compute.models.model_recall_result import (
    EnumRecallConfidence,
    ModelKnowledgeResult,
    ModelRecallResult,
)


class FakeBackend:
    def __init__(
        self,
        results: list[dict[str, object] | ModelKnowledgeResult] | None = None,
        *,
        error: Exception | None = None,
    ) -> None:
        self.results = results or []
        self.error = error
        self.calls: list[tuple[str, ModelRecallFilters | None, int]] = []

    def query(
        self,
        query: str,
        *,
        filters: ModelRecallFilters | None,
        max_results: int,
    ) -> list[dict[str, object] | ModelKnowledgeResult]:
        self.calls.append((query, filters, max_results))
        if self.error is not None:
            raise self.error
        return self.results


@pytest.mark.unit
class TestModelRecallRequest:
    def test_default_scope_is_all(self) -> None:
        req = ModelRecallRequest(query="test query")
        assert req.scope == "all"

    def test_default_max_results_is_five(self) -> None:
        req = ModelRecallRequest(query="test query")
        assert req.max_results == 5

    def test_default_filters_is_none(self) -> None:
        req = ModelRecallRequest(query="test query")
        assert req.filters is None

    def test_scope_override(self) -> None:
        req = ModelRecallRequest(query="test", scope="learnings")
        assert req.scope == "learnings"

    def test_filters_repo(self) -> None:
        filters = ModelRecallFilters(repo="omnibase_infra")
        req = ModelRecallRequest(query="test", filters=filters)
        assert req.filters is not None
        assert req.filters.repo == "omnibase_infra"

    def test_filters_task_type(self) -> None:
        filters = ModelRecallFilters(task_type="ci_fix")
        req = ModelRecallRequest(query="test", filters=filters)
        assert req.filters is not None
        assert req.filters.task_type == "ci_fix"

    def test_request_is_frozen(self) -> None:
        req = ModelRecallRequest(query="test query")
        with pytest.raises(ValidationError):
            req.query = "mutated"  # type: ignore[misc]

    def test_request_rejects_unknown_scope(self) -> None:
        with pytest.raises(ValidationError):
            ModelRecallRequest(query="test", scope="unknown")


@pytest.mark.unit
class TestModelRecallResult:
    def test_default_result_is_empty(self) -> None:
        result = ModelRecallResult()
        assert result.results == ()
        assert result.sources == ()

    def test_default_confidence_is_none(self) -> None:
        result = ModelRecallResult()
        assert result.confidence == EnumRecallConfidence.NONE

    def test_default_partial_is_false(self) -> None:
        result = ModelRecallResult()
        assert result.partial is False

    def test_knowledge_result_fields(self) -> None:
        kr = ModelKnowledgeResult(source="learnings", content="Fixed via X", rank=0)
        assert kr.source == "learnings"
        assert kr.content == "Fixed via X"
        assert kr.rank == 0
        assert kr.similarity is None

    def test_knowledge_result_with_similarity(self) -> None:
        kr = ModelKnowledgeResult(
            source="qdrant", content="Pattern match", rank=1, similarity=0.92
        )
        assert kr.similarity == pytest.approx(0.92)

    def test_result_is_frozen(self) -> None:
        result = ModelRecallResult()
        with pytest.raises(ValidationError):
            result.partial = True  # type: ignore[misc]


@pytest.mark.unit
class TestNodeRecallCompute:
    def test_handler_federates_all_backends_and_ranks_results(self) -> None:
        learnings = FakeBackend(
            [{"content": "fixed kafka import", "similarity": 0.72, "rank": 2}]
        )
        architecture = FakeBackend(
            [{"content": "use event bus", "similarity": 0.91, "rank": 4}]
        )
        antipatterns = FakeBackend(
            [{"content": "avoid localhost bypass", "similarity": 0.83, "rank": 1}]
        )

        result = NodeRecallCompute(
            {
                "learnings": learnings,
                "architecture": architecture,
                "antipatterns": antipatterns,
            }
        ).handle(ModelRecallRequest(query="event bus recall", max_results=2))

        assert [item.content for item in result.results] == [
            "use event bus",
            "avoid localhost bypass",
        ]
        assert [item.rank for item in result.results] == [0, 1]
        assert result.sources == ("antipatterns", "architecture")
        assert result.confidence == EnumRecallConfidence.HIGH
        assert result.partial is False
        assert result.error is None

    def test_handler_applies_scope_and_filters(self) -> None:
        learnings = FakeBackend([{"content": "repo memory", "similarity": 0.6}])
        filters = ModelRecallFilters(repo="omnimarket")

        result = NodeRecallCompute({"learnings": learnings}).handle(
            ModelRecallRequest(
                query="memory",
                scope="learnings",
                filters=filters,
                max_results=3,
            )
        )

        assert result.sources == ("learnings",)
        assert result.confidence == EnumRecallConfidence.MEDIUM
        assert learnings.calls == [("memory", filters, 3)]

    def test_handler_deduplicates_per_source_and_content(self) -> None:
        backend = FakeBackend(
            [
                {"content": "same content", "similarity": 0.2},
                {"content": "same   content", "similarity": 0.9},
            ]
        )

        result = NodeRecallCompute({"architecture": backend}).handle(
            ModelRecallRequest(query="same", scope="architecture")
        )

        assert len(result.results) == 1
        assert result.results[0].similarity == pytest.approx(0.9)

    def test_handler_reports_missing_backend_as_partial(self) -> None:
        result = NodeRecallCompute({"learnings": FakeBackend([])}).handle(
            ModelRecallRequest(query="test", scope="all")
        )

        assert result.partial is True
        assert result.error == "missing backends: architecture, antipatterns"

    def test_handler_reports_backend_failure_as_partial(self) -> None:
        result = NodeRecallCompute(
            {"architecture": FakeBackend(error=RuntimeError("backend down"))}
        ).handle(ModelRecallRequest(query="test", scope="architecture"))

        assert result.partial is True
        assert result.error == "failed backends: architecture: backend down"
        assert result.confidence == EnumRecallConfidence.NONE

    def test_handler_without_backends_returns_controlled_partial_result(self) -> None:
        result = NodeRecallCompute().handle(ModelRecallRequest(query="test"))

        assert result.results == ()
        assert result.partial is True
        assert result.error == "no recall backends configured"
