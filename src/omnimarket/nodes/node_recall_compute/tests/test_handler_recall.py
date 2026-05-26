"""Tests for NodeRecallCompute — stub contract verification."""

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
class TestNodeRecallComputeStub:
    def test_handle_raises_not_implemented(self) -> None:
        handler = NodeRecallCompute()
        req = ModelRecallRequest(query="import error in omnibase_infra")
        with pytest.raises(NotImplementedError):
            handler.handle(req)

    def test_not_implemented_message_cites_ticket(self) -> None:
        handler = NodeRecallCompute()
        req = ModelRecallRequest(query="test")
        with pytest.raises(NotImplementedError, match="OMN-12216"):
            handler.handle(req)

    def test_not_implemented_message_mentions_event_bus(self) -> None:
        handler = NodeRecallCompute()
        req = ModelRecallRequest(query="test")
        with pytest.raises(NotImplementedError, match="event bus"):
            handler.handle(req)

    def test_handler_instantiates(self) -> None:
        handler = NodeRecallCompute()
        assert handler is not None

    def test_stub_raises_for_all_scopes(self) -> None:
        handler = NodeRecallCompute()
        for scope in ("learnings", "architecture", "antipatterns", "all"):
            req = ModelRecallRequest(query="test", scope=scope)
            with pytest.raises(NotImplementedError):
                handler.handle(req)
