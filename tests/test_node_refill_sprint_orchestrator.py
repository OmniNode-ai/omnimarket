"""Unit tests for node_refill_sprint_orchestrator."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from omnimarket.nodes.node_refill_sprint_orchestrator import (
    HandlerRefillSprintOrchestrator,
    ModelBacklogFilter,
    ModelPriorityWeights,
    ModelPulledTicket,
    ModelRefillSprintRequest,
    ModelRefillSprintResult,
    ModelSkippedTicket,
    ModelSprintCapacityConfig,
)

# ---------------------------------------------------------------------------
# Import / public surface
# ---------------------------------------------------------------------------


class TestPublicSurface:
    @pytest.mark.unit
    def test_all_symbols_importable(self) -> None:
        assert HandlerRefillSprintOrchestrator is not None
        assert ModelRefillSprintRequest is not None
        assert ModelRefillSprintResult is not None
        assert ModelSprintCapacityConfig is not None
        assert ModelBacklogFilter is not None
        assert ModelPriorityWeights is not None
        assert ModelPulledTicket is not None
        assert ModelSkippedTicket is not None


# ---------------------------------------------------------------------------
# ModelSprintCapacityConfig
# ---------------------------------------------------------------------------


class TestModelSprintCapacityConfig:
    @pytest.mark.unit
    def test_defaults(self) -> None:
        cfg = ModelSprintCapacityConfig()
        assert cfg.threshold == 5.0
        assert cfg.batch_size == 10
        assert cfg.dry_run is False
        assert cfg.skip_scope_check is False

    @pytest.mark.unit
    def test_custom_values(self) -> None:
        cfg = ModelSprintCapacityConfig(threshold=3.0, batch_size=5, dry_run=True)
        assert cfg.threshold == 3.0
        assert cfg.batch_size == 5
        assert cfg.dry_run is True

    @pytest.mark.unit
    def test_batch_size_lower_bound(self) -> None:
        with pytest.raises(ValidationError):
            ModelSprintCapacityConfig(batch_size=0)

    @pytest.mark.unit
    def test_batch_size_upper_bound(self) -> None:
        with pytest.raises(ValidationError):
            ModelSprintCapacityConfig(batch_size=51)

    @pytest.mark.unit
    def test_threshold_non_negative(self) -> None:
        with pytest.raises(ValidationError):
            ModelSprintCapacityConfig(threshold=-0.1)

    @pytest.mark.unit
    def test_frozen(self) -> None:
        cfg = ModelSprintCapacityConfig()
        with pytest.raises(ValidationError):
            cfg.threshold = 99.0  # type: ignore[misc]

    @pytest.mark.unit
    def test_extra_fields_forbidden(self) -> None:
        with pytest.raises(ValidationError):
            ModelSprintCapacityConfig(unknown="bad")  # type: ignore[call-arg]


# ---------------------------------------------------------------------------
# ModelBacklogFilter
# ---------------------------------------------------------------------------


class TestModelBacklogFilter:
    @pytest.mark.unit
    def test_defaults(self) -> None:
        f = ModelBacklogFilter()
        assert f.team_id is None
        assert f.project_id is None
        assert f.exclude_labels == []
        assert f.include_tier3_keywords is True

    @pytest.mark.unit
    def test_custom_team_and_project(self) -> None:
        f = ModelBacklogFilter(team_id="TEAM-1", project_id="PROJ-2")
        assert f.team_id == "TEAM-1"
        assert f.project_id == "PROJ-2"

    @pytest.mark.unit
    def test_exclude_labels(self) -> None:
        f = ModelBacklogFilter(exclude_labels=["wontfix", "blocked"])
        assert f.exclude_labels == ["wontfix", "blocked"]

    @pytest.mark.unit
    def test_frozen(self) -> None:
        f = ModelBacklogFilter()
        with pytest.raises(ValidationError):
            f.team_id = "X"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# ModelPriorityWeights
# ---------------------------------------------------------------------------


class TestModelPriorityWeights:
    @pytest.mark.unit
    def test_defaults(self) -> None:
        w = ModelPriorityWeights()
        assert w.tier_1 == 3.0
        assert w.tier_2 == 2.0
        assert w.tier_3 == 1.0
        assert w.recency_boost == 0.5

    @pytest.mark.unit
    def test_weights_non_negative(self) -> None:
        with pytest.raises(ValidationError):
            ModelPriorityWeights(tier_1=-1.0)

    @pytest.mark.unit
    def test_zero_weights_allowed(self) -> None:
        # Zero weight disables a tier — allowed per design
        w = ModelPriorityWeights(tier_3=0.0)
        assert w.tier_3 == 0.0

    @pytest.mark.unit
    def test_frozen(self) -> None:
        w = ModelPriorityWeights()
        with pytest.raises(ValidationError):
            w.tier_1 = 99.0  # type: ignore[misc]


# ---------------------------------------------------------------------------
# ModelRefillSprintRequest
# ---------------------------------------------------------------------------


class TestModelRefillSprintRequest:
    @pytest.mark.unit
    def test_default_request_uses_sub_model_defaults(self) -> None:
        req = ModelRefillSprintRequest()
        assert req.capacity_config.threshold == 5.0
        assert req.backlog_filter.team_id is None
        assert req.priority_weights.tier_1 == 3.0

    @pytest.mark.unit
    def test_request_accepts_custom_sub_models(self) -> None:
        req = ModelRefillSprintRequest(
            capacity_config=ModelSprintCapacityConfig(dry_run=True, batch_size=3),
            backlog_filter=ModelBacklogFilter(team_id="ENG"),
            priority_weights=ModelPriorityWeights(tier_1=5.0),
        )
        assert req.capacity_config.dry_run is True
        assert req.backlog_filter.team_id == "ENG"
        assert req.priority_weights.tier_1 == 5.0

    @pytest.mark.unit
    def test_frozen(self) -> None:
        req = ModelRefillSprintRequest()
        with pytest.raises(ValidationError):
            req.capacity_config = ModelSprintCapacityConfig()  # type: ignore[misc]

    @pytest.mark.unit
    def test_extra_fields_forbidden(self) -> None:
        with pytest.raises(ValidationError):
            ModelRefillSprintRequest(surprise="field")  # type: ignore[call-arg]


# ---------------------------------------------------------------------------
# ModelPulledTicket / ModelSkippedTicket
# ---------------------------------------------------------------------------


class TestModelPulledTicket:
    @pytest.mark.unit
    def test_valid_pulled_ticket(self) -> None:
        t = ModelPulledTicket(
            ticket_id="OMN-1234",
            title="Narrow any-type in handler",
            tier=1,
            priority_score=8.5,
            estimate_label="Small",
            scope_verified=True,
        )
        assert t.ticket_id == "OMN-1234"
        assert t.tier == 1
        assert t.scope_verified is True

    @pytest.mark.unit
    def test_tier_bounds(self) -> None:
        with pytest.raises(ValidationError):
            ModelPulledTicket(
                ticket_id="X",
                title="X",
                tier=0,  # out of range
                priority_score=1.0,
                scope_verified=True,
            )
        with pytest.raises(ValidationError):
            ModelPulledTicket(
                ticket_id="X",
                title="X",
                tier=4,  # out of range
                priority_score=1.0,
                scope_verified=True,
            )

    @pytest.mark.unit
    def test_frozen(self) -> None:
        t = ModelPulledTicket(
            ticket_id="OMN-1",
            title="T",
            tier=2,
            priority_score=2.0,
            scope_verified=False,
        )
        with pytest.raises(ValidationError):
            t.tier = 1  # type: ignore[misc]


class TestModelSkippedTicket:
    @pytest.mark.unit
    def test_valid_skipped_ticket(self) -> None:
        s = ModelSkippedTicket(
            ticket_id="OMN-5678",
            title="Refactor legacy auth",
            reason="estimate_too_large",
        )
        assert s.reason == "estimate_too_large"

    @pytest.mark.unit
    def test_frozen(self) -> None:
        s = ModelSkippedTicket(ticket_id="X", title="Y", reason="zombie_ticket")
        with pytest.raises(ValidationError):
            s.reason = "other"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# ModelRefillSprintResult
# ---------------------------------------------------------------------------


class TestModelRefillSprintResult:
    @pytest.mark.unit
    def test_empty_result(self) -> None:
        r = ModelRefillSprintResult(
            pulled_count=0,
            skipped_count=0,
            exhausted=True,
            capacity_before=6.0,
            capacity_after=6.0,
            dry_run=False,
        )
        assert r.pulled == []
        assert r.skipped == []
        assert r.exhausted is True

    @pytest.mark.unit
    def test_result_with_pulled_tickets(self) -> None:
        pulled = [
            ModelPulledTicket(
                ticket_id="OMN-100",
                title="Fix lint suppression",
                tier=1,
                priority_score=9.0,
                scope_verified=True,
            )
        ]
        r = ModelRefillSprintResult(
            pulled=pulled,
            pulled_count=1,
            skipped_count=0,
            exhausted=False,
            capacity_before=2.0,
            capacity_after=3.0,
            dry_run=False,
        )
        assert r.pulled_count == 1
        assert r.pulled[0].ticket_id == "OMN-100"

    @pytest.mark.unit
    def test_capacity_non_negative(self) -> None:
        with pytest.raises(ValidationError):
            ModelRefillSprintResult(
                pulled_count=0,
                skipped_count=0,
                exhausted=False,
                capacity_before=-1.0,
                capacity_after=0.0,
                dry_run=False,
            )


# ---------------------------------------------------------------------------
# Handler
# ---------------------------------------------------------------------------


class _BacklogAdapter:
    def current_capacity(self) -> float:
        return 1.0

    def candidate_tickets(
        self, backlog_filter: ModelBacklogFilter
    ) -> list[dict[str, object]]:
        return [
            {
                "ticket_id": "OMN-1",
                "title": "Narrow any type",
                "tier": 1,
                "age_weeks": 2,
                "scope_verified": True,
            },
            {
                "ticket_id": "OMN-2",
                "title": "Large rewrite",
                "tier": 2,
                "estimate_too_large": True,
            },
        ]

    def pull_ticket(self, ticket_id: str) -> None:
        raise AssertionError("dry_run must not pull tickets")


class TestHandlerRefillSprintOrchestrator:
    @pytest.mark.unit
    def test_dry_run_without_adapter_returns_empty_exhausted_result(self) -> None:
        handler = HandlerRefillSprintOrchestrator()
        req = ModelRefillSprintRequest(
            capacity_config=ModelSprintCapacityConfig(dry_run=True)
        )
        result = handler.handle(req)

        assert result.dry_run is True
        assert result.exhausted is True
        assert result.pulled_count == 0

    @pytest.mark.unit
    def test_handler_instantiates_without_args(self) -> None:
        handler = HandlerRefillSprintOrchestrator()
        assert handler is not None

    @pytest.mark.unit
    def test_live_without_adapter_requires_adapter(self) -> None:
        handler = HandlerRefillSprintOrchestrator()

        with pytest.raises(RuntimeError, match="backlog adapter required"):
            handler.handle(ModelRefillSprintRequest())

    @pytest.mark.unit
    def test_dry_run_scores_injected_candidates_without_mutation(self) -> None:
        handler = HandlerRefillSprintOrchestrator(adapter=_BacklogAdapter())
        req = ModelRefillSprintRequest(
            capacity_config=ModelSprintCapacityConfig(dry_run=True, batch_size=1)
        )

        result = handler.handle(req)

        assert result.dry_run is True
        assert result.pulled_count == 1
        assert result.pulled[0].ticket_id == "OMN-1"
        assert result.pulled[0].priority_score == 4.0
        assert result.skipped_count == 1
