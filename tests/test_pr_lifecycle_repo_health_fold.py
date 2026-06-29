# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Fold tests for the additive repo_health sub-record on ModelPrLifecycleState.

DoD (OMN-13585):
* Diff shows additive-only change; existing PR-lane fields untouched (byte-identical).
* Fold: repo-health-classified and repo-health-repair-emitted events update only
  repo_health.* — all existing PR-lane field values are byte-identical before/after.
* Frozen ConfigDict + extra="forbid" retained on both models.
* uv run pytest tests/ (no -k filter) green; mypy --strict clean; pre-commit clean.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from omnimarket.nodes.node_pr_lifecycle_state_reducer.handlers.handler_pr_lifecycle_state_reducer import (
    HandlerPrLifecycleStateReducer,
    fold_repo_health_classified,
    fold_repo_health_repair_emitted,
)
from omnimarket.nodes.node_pr_lifecycle_state_reducer.models.model_pr_lifecycle_state import (
    ModelPrLifecycleState,
    ModelRepoHealthLaneState,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _base_state(**kwargs: object) -> ModelPrLifecycleState:
    """Return a minimal, valid ModelPrLifecycleState."""
    cid = kwargs.pop("correlation_id", uuid4())  # type: ignore[arg-type]
    return ModelPrLifecycleState(correlation_id=cid, **kwargs)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# ModelRepoHealthLaneState — unit tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestModelRepoHealthLaneState:
    """ModelRepoHealthLaneState construction, defaults, and immutability."""

    def test_default_empty_construction(self) -> None:
        """Default instance has all counters zero and empty refs tuple."""
        rh = ModelRepoHealthLaneState()
        assert rh.classified_count == 0
        assert rh.pr_scoped_count == 0
        assert rh.repo_baseline_count == 0
        assert rh.external_dependency_count == 0
        assert rh.unknown_count == 0
        assert rh.repair_tasks_emitted == 0
        assert rh.repair_task_refs == ()

    def test_frozen_rejects_mutation(self) -> None:
        """Frozen model raises ValidationError / TypeError on in-place mutation."""
        rh = ModelRepoHealthLaneState()
        with pytest.raises((TypeError, Exception)):
            rh.classified_count = 5  # type: ignore[misc]

    def test_extra_forbid_rejects_unknown_fields(self) -> None:
        """extra='forbid' rejects unknown fields at construction."""
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            ModelRepoHealthLaneState(unknown_field=99)  # type: ignore[call-arg]

    def test_construct_with_values(self) -> None:
        """ModelRepoHealthLaneState accepts all declared fields."""
        rh = ModelRepoHealthLaneState(
            classified_count=3,
            pr_scoped_count=1,
            repo_baseline_count=1,
            external_dependency_count=1,
            unknown_count=0,
            repair_tasks_emitted=2,
            repair_task_refs=("OMN-9001", "OMN-9002"),
        )
        assert rh.classified_count == 3
        assert rh.repair_task_refs == ("OMN-9001", "OMN-9002")

    def test_repair_task_refs_dedup_on_copy(self) -> None:
        """model_copy(update=...) with duplicate refs stays deduplicated by fold logic."""
        rh = ModelRepoHealthLaneState(
            repair_task_refs=("OMN-9001",),
        )
        # Simulate adding same ref again — the fold function must deduplicate
        new_refs = tuple(dict.fromkeys((*rh.repair_task_refs, "OMN-9001")))
        updated = rh.model_copy(update={"repair_task_refs": new_refs})
        assert updated.repair_task_refs == ("OMN-9001",)


# ---------------------------------------------------------------------------
# ModelPrLifecycleState — additive extension tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestModelPrLifecycleStateAdditiveExtension:
    """repo_health sub-record is additive; existing fields are byte-identical."""

    def test_repo_health_field_exists_with_default(self) -> None:
        """ModelPrLifecycleState carries a repo_health field with a zero default."""
        state = _base_state()
        assert hasattr(state, "repo_health")
        assert isinstance(state.repo_health, ModelRepoHealthLaneState)
        assert state.repo_health.classified_count == 0

    def test_existing_pr_lane_fields_unaffected_by_repo_health_default(self) -> None:
        """Existing PR-lane scalar fields have the same defaults as before the change."""
        state = _base_state()
        # These are the original fields — must remain at their spec'd defaults
        assert state.prs_inventoried == 0
        assert state.prs_blocked == 0
        assert state.prs_fixed == 0
        assert state.prs_merged == 0
        assert state.prs_processed == 0
        assert state.error_message is None
        assert state.started_at is None
        assert state.last_phase_at is None

    def test_model_extra_forbid_still_enforced(self) -> None:
        """extra='forbid' is retained on ModelPrLifecycleState after extension."""
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            ModelPrLifecycleState(
                correlation_id=uuid4(),
                unknown_extra_field="bad",  # type: ignore[call-arg]
            )

    def test_frozen_still_enforced(self) -> None:
        """ModelPrLifecycleState is still frozen after the extension."""
        state = _base_state()
        with pytest.raises((TypeError, Exception)):
            state.prs_inventoried = 99  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Fold function tests — additive isolation is the core DoD
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestRepoHealthFoldFunctions:
    """fold_repo_health_classified and fold_repo_health_repair_emitted tests."""

    def test_fold_classified_increments_classified_count(self) -> None:
        """fold_repo_health_classified increments classified_count by 1."""
        state = _base_state()
        new_state = fold_repo_health_classified(
            state,
            classification="pr_scoped",
            ticket_ref=None,
        )
        assert new_state.repo_health.classified_count == 1

    def test_fold_classified_pr_scoped_increments_pr_scoped_count(self) -> None:
        """classification='pr_scoped' increments pr_scoped_count."""
        state = _base_state()
        new_state = fold_repo_health_classified(
            state,
            classification="pr_scoped",
            ticket_ref=None,
        )
        assert new_state.repo_health.pr_scoped_count == 1
        assert new_state.repo_health.repo_baseline_count == 0
        assert new_state.repo_health.external_dependency_count == 0
        assert new_state.repo_health.unknown_count == 0

    def test_fold_classified_repo_baseline_increments_repo_baseline_count(self) -> None:
        """classification='repo_baseline' increments repo_baseline_count."""
        state = _base_state()
        new_state = fold_repo_health_classified(
            state,
            classification="repo_baseline",
            ticket_ref=None,
        )
        assert new_state.repo_health.repo_baseline_count == 1
        assert new_state.repo_health.pr_scoped_count == 0

    def test_fold_classified_external_increments_external_count(self) -> None:
        """classification='external_dependency' increments external_dependency_count."""
        state = _base_state()
        new_state = fold_repo_health_classified(
            state,
            classification="external_dependency",
            ticket_ref=None,
        )
        assert new_state.repo_health.external_dependency_count == 1

    def test_fold_classified_unknown_increments_unknown_count(self) -> None:
        """classification='unknown' increments unknown_count."""
        state = _base_state()
        new_state = fold_repo_health_classified(
            state,
            classification="unknown",
            ticket_ref=None,
        )
        assert new_state.repo_health.unknown_count == 1

    def test_fold_repair_emitted_increments_repair_tasks_emitted(self) -> None:
        """fold_repo_health_repair_emitted increments repair_tasks_emitted."""
        state = _base_state()
        new_state = fold_repo_health_repair_emitted(
            state,
            ticket_ref="OMN-9001",
        )
        assert new_state.repo_health.repair_tasks_emitted == 1

    def test_fold_repair_emitted_adds_ticket_ref(self) -> None:
        """fold_repo_health_repair_emitted appends ticket_ref to repair_task_refs."""
        state = _base_state()
        new_state = fold_repo_health_repair_emitted(
            state,
            ticket_ref="OMN-9001",
        )
        assert "OMN-9001" in new_state.repo_health.repair_task_refs

    def test_fold_repair_emitted_deduplicates_refs(self) -> None:
        """Duplicate ticket_refs are deduplicated in repair_task_refs."""
        state = _base_state()
        s1 = fold_repo_health_repair_emitted(state, ticket_ref="OMN-9001")
        s2 = fold_repo_health_repair_emitted(s1, ticket_ref="OMN-9001")
        assert s2.repo_health.repair_task_refs.count("OMN-9001") == 1

    def test_fold_repair_emitted_allows_none_ref(self) -> None:
        """fold_repo_health_repair_emitted accepts None ticket_ref (no ref added)."""
        state = _base_state()
        new_state = fold_repo_health_repair_emitted(state, ticket_ref=None)
        assert new_state.repo_health.repair_tasks_emitted == 1
        assert new_state.repo_health.repair_task_refs == ()

    # -----------------------------------------------------------------------
    # CORE DoD: existing PR-lane fields are BYTE-IDENTICAL after repo_health folds
    # -----------------------------------------------------------------------

    def test_classified_fold_leaves_pr_lane_fields_byte_identical(self) -> None:
        """After fold_repo_health_classified, all existing PR-lane fields are unchanged."""
        from omnimarket.nodes.node_pr_lifecycle_state_reducer.models.model_pr_lifecycle_event import (
            EnumPrLifecyclePhase,
        )

        ts = datetime.now(tz=UTC)
        cid = uuid4()
        state = ModelPrLifecycleState(
            correlation_id=cid,
            phase=EnumPrLifecyclePhase.INVENTORYING,
            prs_inventoried=5,
            prs_blocked=2,
            prs_fixed=1,
            prs_merged=0,
            prs_processed=0,
            started_at=ts,
            last_phase_at=ts,
            error_message=None,
        )

        new_state = fold_repo_health_classified(
            state, classification="pr_scoped", ticket_ref=None
        )

        # All existing PR-lane scalars must be byte-identical
        assert new_state.correlation_id == state.correlation_id
        assert new_state.phase == state.phase
        assert new_state.prs_inventoried == state.prs_inventoried
        assert new_state.prs_blocked == state.prs_blocked
        assert new_state.prs_fixed == state.prs_fixed
        assert new_state.prs_merged == state.prs_merged
        assert new_state.prs_processed == state.prs_processed
        assert new_state.started_at == state.started_at
        assert new_state.last_phase_at == state.last_phase_at
        assert new_state.error_message == state.error_message
        assert new_state.entry_flags == state.entry_flags

        # Only repo_health changed
        assert new_state.repo_health.classified_count == 1

    def test_repair_emitted_fold_leaves_pr_lane_fields_byte_identical(self) -> None:
        """After fold_repo_health_repair_emitted, all existing PR-lane fields are unchanged."""
        from omnimarket.nodes.node_pr_lifecycle_state_reducer.models.model_pr_lifecycle_event import (
            EnumPrLifecyclePhase,
        )

        ts = datetime.now(tz=UTC)
        cid = uuid4()
        state = ModelPrLifecycleState(
            correlation_id=cid,
            phase=EnumPrLifecyclePhase.TRIAGED,
            prs_inventoried=10,
            prs_blocked=3,
            prs_fixed=0,
            prs_merged=0,
            prs_processed=0,
            started_at=ts,
            last_phase_at=ts,
            error_message=None,
        )

        new_state = fold_repo_health_repair_emitted(state, ticket_ref="OMN-9999")

        # All existing PR-lane scalars must be byte-identical
        assert new_state.correlation_id == state.correlation_id
        assert new_state.phase == state.phase
        assert new_state.prs_inventoried == state.prs_inventoried
        assert new_state.prs_blocked == state.prs_blocked
        assert new_state.prs_fixed == state.prs_fixed
        assert new_state.prs_merged == state.prs_merged
        assert new_state.prs_processed == state.prs_processed
        assert new_state.started_at == state.started_at
        assert new_state.last_phase_at == state.last_phase_at
        assert new_state.error_message == state.error_message
        assert new_state.entry_flags == state.entry_flags

        # Only repo_health changed
        assert new_state.repo_health.repair_tasks_emitted == 1
        assert new_state.repo_health.repair_task_refs == ("OMN-9999",)

    def test_multiple_classified_events_accumulate(self) -> None:
        """Multiple fold_repo_health_classified calls accumulate counts correctly."""
        state = _base_state()
        state = fold_repo_health_classified(
            state, classification="pr_scoped", ticket_ref=None
        )
        state = fold_repo_health_classified(
            state, classification="pr_scoped", ticket_ref=None
        )
        state = fold_repo_health_classified(
            state, classification="repo_baseline", ticket_ref=None
        )
        state = fold_repo_health_classified(
            state, classification="unknown", ticket_ref=None
        )

        assert state.repo_health.classified_count == 4
        assert state.repo_health.pr_scoped_count == 2
        assert state.repo_health.repo_baseline_count == 1
        assert state.repo_health.unknown_count == 1

    def test_mixed_folds_accumulate_independently(self) -> None:
        """Mixed classified + repair-emitted events accumulate in their own sub-fields."""
        state = _base_state()
        state = fold_repo_health_classified(
            state, classification="pr_scoped", ticket_ref=None
        )
        state = fold_repo_health_repair_emitted(state, ticket_ref="OMN-9001")
        state = fold_repo_health_classified(
            state, classification="external_dependency", ticket_ref=None
        )
        state = fold_repo_health_repair_emitted(state, ticket_ref="OMN-9002")

        assert state.repo_health.classified_count == 2
        assert state.repo_health.pr_scoped_count == 1
        assert state.repo_health.external_dependency_count == 1
        assert state.repo_health.repair_tasks_emitted == 2
        assert set(state.repo_health.repair_task_refs) == {"OMN-9001", "OMN-9002"}


# ---------------------------------------------------------------------------
# HandlerPrLifecycleStateReducer — handle_dict dispatch for repo_health events
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestHandlerFoldsRepoHealthViaHandleDict:
    """handle_dict dispatches repo-health events to fold functions."""

    def test_handle_dict_repo_health_classified_updates_only_repo_health(self) -> None:
        """handle_dict with repo-health-classified topic updates only repo_health.*."""
        handler = HandlerPrLifecycleStateReducer()
        cid = str(uuid4())
        input_data = {
            "event_topic": "onex.evt.omnimarket.repo-health-classified.v1",
            "state": {
                "correlation_id": cid,
                "prs_inventoried": 7,
                "prs_blocked": 3,
            },
            "event": {
                "classification": "pr_scoped",
                "ticket_ref": None,
            },
        }
        result = handler.handle_dict(input_data)
        state_out = result["state"]

        # repo_health updated
        assert state_out["repo_health"]["classified_count"] == 1
        assert state_out["repo_health"]["pr_scoped_count"] == 1
        # PR-lane fields byte-identical
        assert state_out["prs_inventoried"] == 7
        assert state_out["prs_blocked"] == 3

    def test_handle_dict_repo_health_repair_emitted_updates_only_repo_health(
        self,
    ) -> None:
        """handle_dict with repo-health-repair-emitted topic updates only repo_health.*."""
        handler = HandlerPrLifecycleStateReducer()
        cid = str(uuid4())
        input_data = {
            "event_topic": "onex.evt.omnimarket.repo-health-repair-emitted.v1",
            "state": {
                "correlation_id": cid,
                "prs_inventoried": 4,
                "prs_merged": 2,
            },
            "event": {
                "ticket_ref": "OMN-9001",
            },
        }
        result = handler.handle_dict(input_data)
        state_out = result["state"]

        # repo_health updated
        assert state_out["repo_health"]["repair_tasks_emitted"] == 1
        assert "OMN-9001" in state_out["repo_health"]["repair_task_refs"]
        # PR-lane fields byte-identical
        assert state_out["prs_inventoried"] == 4
        assert state_out["prs_merged"] == 2
