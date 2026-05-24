"""Tests for HandlerSwarmRegistry — pure deterministic endpoint selection."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest
from pydantic import ValidationError

from omnimarket.nodes.node_swarm_registry_compute.handlers.handler_swarm_registry import (
    HandlerSwarmRegistry,
    _load_registry,
)
from omnimarket.nodes.node_swarm_registry_compute.models.enums import (
    EnumEndpointStatus,
    EnumModelStatus,
)
from omnimarket.nodes.node_swarm_registry_compute.models.model_swarm_endpoint_selection_request import (
    ModelEndpointHealth,
    ModelSubtask,
    ModelSwarmEndpointSelectionRequest,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_REGISTRY_YAML = textwrap.dedent(
    """\
    registry_schema_version: "1.0.0"
    endpoints:
      - id: "code-ep"
        base_url: "http://localhost:8000/v1"
        model_id: "some-coder"
        provider: "vllm"
        capabilities: [code_generation, refactoring, analysis]
        context_window: 100000
        cost_basis: "local"
      - id: "reason-ep"
        base_url: "http://localhost:8001/v1"
        model_id: "some-reasoner"
        provider: "vllm"
        capabilities: [reasoning, math, analysis]
        context_window: 24000
        cost_basis: "local"
      - id: "general-ep"
        base_url: "http://localhost:8002/v1"
        model_id: "some-general"
        provider: "mlx"
        capabilities: [general, synthesis, planning, code_generation]
        context_window: null
        cost_basis: "local"
    """
)


@pytest.fixture
def registry_file(tmp_path: Path) -> Path:
    p = tmp_path / "endpoint_registry.yaml"
    p.write_text(_REGISTRY_YAML)
    return p


@pytest.fixture
def handler(registry_file: Path) -> HandlerSwarmRegistry:
    return HandlerSwarmRegistry(registry_path=registry_file)


def _health(
    endpoint_id: str,
    *,
    status: EnumEndpointStatus = EnumEndpointStatus.reachable,
    model_status: EnumModelStatus = EnumModelStatus.available,
) -> ModelEndpointHealth:
    return ModelEndpointHealth(
        endpoint_id=endpoint_id,
        endpoint_status=status,
        model_status=model_status,
    )


def _all_healthy() -> dict[str, ModelEndpointHealth]:
    return {
        "code-ep": _health("code-ep"),
        "reason-ep": _health("reason-ep"),
        "general-ep": _health("general-ep"),
    }


def _subtask(
    subtask_id: str,
    category: str,
    *,
    estimated_tokens: int = 0,
    model_affinity: str = "",
) -> ModelSubtask:
    return ModelSubtask(
        subtask_id=subtask_id,
        description=f"Task {subtask_id}",
        category=category,
        estimated_tokens=estimated_tokens,
        model_affinity=model_affinity,
    )


def _request(
    subtasks: list[ModelSubtask],
    endpoint_health: dict[str, ModelEndpointHealth] | None = None,
    registry_hash: str = "abc123",
) -> ModelSwarmEndpointSelectionRequest:
    return ModelSwarmEndpointSelectionRequest(
        subtasks=tuple(subtasks),
        endpoint_health=endpoint_health
        if endpoint_health is not None
        else _all_healthy(),
        registry_hash=registry_hash,
    )


# ---------------------------------------------------------------------------
# Tests: affinity matching
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestAffinityMatching:
    def test_explicit_affinity_is_used_when_healthy_and_capable(
        self, handler: HandlerSwarmRegistry
    ) -> None:
        req = _request([_subtask("t1", "code_generation", model_affinity="code-ep")])
        result = handler.handle(req)
        assert result.assignments["t1"] == "code-ep"

    def test_affinity_reason_mentions_affinity_match(
        self, handler: HandlerSwarmRegistry
    ) -> None:
        req = _request([_subtask("t1", "code_generation", model_affinity="code-ep")])
        result = handler.handle(req)
        ev = next(e for e in result.selection_evidence if e.subtask_id == "t1")
        assert "affinity" in ev.reason

    def test_affinity_skipped_when_endpoint_unhealthy(
        self, handler: HandlerSwarmRegistry
    ) -> None:
        health = _all_healthy()
        health["code-ep"] = _health("code-ep", status=EnumEndpointStatus.unreachable)
        req = _request(
            [_subtask("t1", "code_generation", model_affinity="code-ep")],
            endpoint_health=health,
        )
        result = handler.handle(req)
        # Falls back to general-ep which also has code_generation
        assert result.assignments.get("t1") == "general-ep"

    def test_affinity_skipped_when_category_not_in_capabilities(
        self, handler: HandlerSwarmRegistry
    ) -> None:
        # code-ep has no "reasoning" capability
        req = _request([_subtask("t1", "reasoning", model_affinity="code-ep")])
        result = handler.handle(req)
        assert result.assignments.get("t1") == "reason-ep"

    def test_affinity_skipped_when_context_too_small(
        self, handler: HandlerSwarmRegistry
    ) -> None:
        # reason-ep has context_window=24000; request 50000 tokens
        req = _request(
            [
                _subtask(
                    "t1",
                    "reasoning",
                    estimated_tokens=50000,
                    model_affinity="reason-ep",
                )
            ]
        )
        result = handler.handle(req)
        # reason-ep can't fit; no other reasoning ep with known ctx → falls to unknown ctx (none here)
        # All reasoning eps: reason-ep (24000 < 50000), no others → unroutable
        assert "t1" in result.unroutable_subtasks


# ---------------------------------------------------------------------------
# Tests: capability matching
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestCapabilityMatching:
    def test_no_affinity_routes_by_capability(
        self, handler: HandlerSwarmRegistry
    ) -> None:
        req = _request([_subtask("t1", "reasoning")])
        result = handler.handle(req)
        assert result.assignments["t1"] == "reason-ep"

    def test_capability_match_reason_mentions_capability(
        self, handler: HandlerSwarmRegistry
    ) -> None:
        req = _request([_subtask("t1", "reasoning")])
        result = handler.handle(req)
        ev = next(e for e in result.selection_evidence if e.subtask_id == "t1")
        assert "capability" in ev.reason

    def test_known_context_preferred_over_unknown(
        self, handler: HandlerSwarmRegistry
    ) -> None:
        # code_generation: code-ep (100000) and general-ep (null)
        # code-ep has known ctx that fits → should win
        req = _request([_subtask("t1", "code_generation", estimated_tokens=50000)])
        result = handler.handle(req)
        assert result.assignments["t1"] == "code-ep"

    def test_unknown_context_used_when_no_known_ctx_fits(
        self, handler: HandlerSwarmRegistry
    ) -> None:
        # synthesis: only general-ep (null ctx)
        req = _request([_subtask("t1", "synthesis", estimated_tokens=999999)])
        result = handler.handle(req)
        assert result.assignments["t1"] == "general-ep"

    def test_multiple_subtasks_routed_independently(
        self, handler: HandlerSwarmRegistry
    ) -> None:
        req = _request(
            [
                _subtask("t1", "code_generation"),
                _subtask("t2", "reasoning"),
                _subtask("t3", "synthesis"),
            ]
        )
        result = handler.handle(req)
        assert result.assignments["t1"] == "code-ep"
        assert result.assignments["t2"] == "reason-ep"
        assert result.assignments["t3"] == "general-ep"


# ---------------------------------------------------------------------------
# Tests: unhealthy endpoints filtered out
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestHealthFiltering:
    def test_unreachable_endpoint_not_selected(
        self, handler: HandlerSwarmRegistry
    ) -> None:
        health = _all_healthy()
        health["reason-ep"] = _health(
            "reason-ep", status=EnumEndpointStatus.unreachable
        )
        req = _request([_subtask("t1", "math")], endpoint_health=health)
        # math only on reason-ep; it's unhealthy → unroutable
        result = handler.handle(req)
        assert "t1" in result.unroutable_subtasks

    def test_timeout_endpoint_treated_as_unhealthy(
        self, handler: HandlerSwarmRegistry
    ) -> None:
        health = _all_healthy()
        health["reason-ep"] = _health("reason-ep", status=EnumEndpointStatus.timeout)
        req = _request([_subtask("t1", "math")], endpoint_health=health)
        result = handler.handle(req)
        assert "t1" in result.unroutable_subtasks

    def test_missing_health_entry_treated_as_unhealthy(
        self, handler: HandlerSwarmRegistry
    ) -> None:
        # Remove reason-ep from health dict entirely
        health = {"code-ep": _health("code-ep"), "general-ep": _health("general-ep")}
        req = _request([_subtask("t1", "math")], endpoint_health=health)
        result = handler.handle(req)
        assert "t1" in result.unroutable_subtasks

    def test_all_endpoints_unhealthy_all_subtasks_unroutable(
        self, handler: HandlerSwarmRegistry
    ) -> None:
        health = {
            "code-ep": _health("code-ep", status=EnumEndpointStatus.unreachable),
            "reason-ep": _health("reason-ep", status=EnumEndpointStatus.unreachable),
            "general-ep": _health("general-ep", status=EnumEndpointStatus.unreachable),
        }
        req = _request(
            [_subtask("t1", "code_generation"), _subtask("t2", "reasoning")],
            endpoint_health=health,
        )
        result = handler.handle(req)
        assert set(result.unroutable_subtasks) == {"t1", "t2"}
        assert result.assignments == {}


# ---------------------------------------------------------------------------
# Tests: unroutable subtasks
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestUnroutableSubtasks:
    def test_unknown_category_is_unroutable(
        self, handler: HandlerSwarmRegistry
    ) -> None:
        req = _request([_subtask("t1", "some_unknown_category")])
        result = handler.handle(req)
        assert "t1" in result.unroutable_subtasks
        assert "t1" not in result.assignments

    def test_unroutable_not_in_assignments(self, handler: HandlerSwarmRegistry) -> None:
        req = _request([_subtask("t1", "nonexistent")])
        result = handler.handle(req)
        assert "t1" not in result.assignments

    def test_partial_routing_routable_and_unroutable_mixed(
        self, handler: HandlerSwarmRegistry
    ) -> None:
        req = _request(
            [
                _subtask("t1", "code_generation"),
                _subtask("t2", "nonexistent_category"),
            ]
        )
        result = handler.handle(req)
        assert "t1" in result.assignments
        assert "t2" in result.unroutable_subtasks


# ---------------------------------------------------------------------------
# Tests: selection evidence
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestSelectionEvidence:
    def test_evidence_recorded_for_each_routed_subtask(
        self, handler: HandlerSwarmRegistry
    ) -> None:
        req = _request([_subtask("t1", "code_generation"), _subtask("t2", "reasoning")])
        result = handler.handle(req)
        evidence_ids = {e.subtask_id for e in result.selection_evidence}
        assert evidence_ids == {"t1", "t2"}

    def test_evidence_not_recorded_for_unroutable(
        self, handler: HandlerSwarmRegistry
    ) -> None:
        req = _request([_subtask("t1", "nonexistent")])
        result = handler.handle(req)
        assert all(e.subtask_id != "t1" for e in result.selection_evidence)

    def test_evidence_assigned_endpoint_matches_assignment(
        self, handler: HandlerSwarmRegistry
    ) -> None:
        req = _request([_subtask("t1", "code_generation")])
        result = handler.handle(req)
        ev = next(e for e in result.selection_evidence if e.subtask_id == "t1")
        assert ev.assigned_endpoint_id == result.assignments["t1"]

    def test_evidence_reason_is_non_empty(self, handler: HandlerSwarmRegistry) -> None:
        req = _request([_subtask("t1", "reasoning")])
        result = handler.handle(req)
        ev = next(e for e in result.selection_evidence if e.subtask_id == "t1")
        assert ev.reason


# ---------------------------------------------------------------------------
# Tests: registry loading
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestRegistryLoading:
    def test_default_registry_loads_without_error(self) -> None:
        handler = HandlerSwarmRegistry()
        assert handler is not None

    def test_custom_registry_path_loads(self, registry_file: Path) -> None:
        handler = HandlerSwarmRegistry(registry_path=registry_file)
        assert handler is not None

    def test_duplicate_endpoint_id_raises(self, tmp_path: Path) -> None:
        dup_yaml = textwrap.dedent(
            """\
            registry_schema_version: "1.0.0"
            endpoints:
              - id: "dup"
                base_url: "http://localhost:8000/v1"
                model_id: "m1"
                provider: "vllm"
                capabilities: [general]
              - id: "dup"
                base_url: "http://localhost:8001/v1"
                model_id: "m2"
                provider: "vllm"
                capabilities: [general]
            """
        )
        p = tmp_path / "bad.yaml"
        p.write_text(dup_yaml)
        with pytest.raises(ValueError, match="Duplicate endpoint id"):
            _load_registry(p)

    def test_invalid_base_url_raises(self, tmp_path: Path) -> None:
        bad_yaml = textwrap.dedent(
            """\
            registry_schema_version: "1.0.0"
            endpoints:
              - id: "bad"
                base_url: "not-a-url"
                model_id: "m1"
                provider: "vllm"
                capabilities: [general]
            """
        )
        p = tmp_path / "bad.yaml"
        p.write_text(bad_yaml)
        with pytest.raises(ValueError, match="Unparseable base_url"):
            _load_registry(p)

    def test_unknown_capability_raises(self, tmp_path: Path) -> None:
        bad_yaml = textwrap.dedent(
            """\
            registry_schema_version: "1.0.0"
            endpoints:
              - id: "ep"
                base_url: "http://localhost:8000/v1"
                model_id: "m1"
                provider: "vllm"
                capabilities: [general, totally_fake_cap]
            """
        )
        p = tmp_path / "bad.yaml"
        p.write_text(bad_yaml)
        with pytest.raises(ValueError, match="Unknown capabilities"):
            _load_registry(p)


# ---------------------------------------------------------------------------
# Tests: model validation
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestModelValidation:
    def test_subtask_frozen(self) -> None:
        s = _subtask("t1", "general")
        with pytest.raises((TypeError, AttributeError, ValidationError)):
            s.subtask_id = "t2"  # type: ignore[misc]

    def test_endpoint_health_frozen(self) -> None:
        h = _health("ep1")
        with pytest.raises((TypeError, AttributeError, ValidationError)):
            h.endpoint_id = "ep2"  # type: ignore[misc]

    def test_result_frozen(self, handler: HandlerSwarmRegistry) -> None:
        req = _request([_subtask("t1", "reasoning")])
        result = handler.handle(req)
        with pytest.raises((TypeError, AttributeError, ValidationError)):
            result.assignments = {}  # type: ignore[misc]

    def test_same_inputs_produce_identical_outputs(
        self, handler: HandlerSwarmRegistry
    ) -> None:
        req = _request([_subtask("t1", "code_generation"), _subtask("t2", "reasoning")])
        r1 = handler.handle(req)
        r2 = handler.handle(req)
        assert r1.assignments == r2.assignments
        assert r1.unroutable_subtasks == r2.unroutable_subtasks
