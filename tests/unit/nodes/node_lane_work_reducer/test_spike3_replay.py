# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Spike 3 as replay: each recorded red CI log is classified, then folded from `red` (OMN-26698).

The fixtures are the failing step of each job as CI recorded it: omnimarket#6573 (file shape
ratchet), #7030 (overlay missing on the overlay store main, and dod-verify). The classifier reads the log with the
bootstrap overlay; the reducer folds the event it returns from the `red` state. The route is what
the plan names for each change.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from omnibase_core.enums.enum_lane_work_event import EnumLaneWorkEvent
from omnibase_core.enums.enum_lane_work_state import EnumLaneWorkState
from omnibase_core.models.lane_work import ModelLaneWorkChange, ModelLaneWorkState

from omnimarket.nodes.node_ci_red_kind_rule_compute.handlers.handler_ci_red_kind_rule import (
    HandlerCiRedKindRule,
)
from omnimarket.nodes.node_ci_red_kind_rule_compute.models.model_ci_red_kind_rule_overlay import (
    ModelCiRedKindRuleOverlay,
)
from omnimarket.nodes.node_lane_work_event_classify_compute.handlers.handler_lane_work_event_classify import (
    HandlerLaneWorkEventClassify,
)
from omnimarket.nodes.node_lane_work_event_classify_compute.models.model_lane_work_classify_overlay import (
    ModelLaneWorkClassifyOverlay,
)

from .conftest_lane_work import TIERS, Driver

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[4]
FIXTURES = ROOT / "tests" / "fixtures" / "lane_work_spike3" / "2026-11-27"
OVERLAY = (
    ROOT
    / "src/omnimarket/nodes/node_lane_work_event_classify_compute/overlay.example.yaml"
)
HEAD = "0dbe3156e52dcb33b15fc3c82f5959de6691a4de"

LOGS = {
    "6573": "omnimarket_6573_canonical_file_shape_job_139519889541.txt",
    "7030-overlay": "omnimarket_7030_overlay_main_load_job_140823465576.txt",
    "7030-dod": "omnimarket_7030_dod_verify_job_140823464890.txt",
}


def _classifier() -> HandlerLaneWorkEventClassify:
    parsed = ModelLaneWorkClassifyOverlay.model_validate(
        yaml.safe_load(OVERLAY.read_text())
    )
    red = HandlerCiRedKindRule(
        load_overlay=lambda: ModelCiRedKindRuleOverlay.model_validate(
            {"schema_version": "ci_red_kind_rules.v1", "rules": []}
        )
    )
    return HandlerLaneWorkEventClassify(load_overlay=lambda: parsed, red_kind_rule=red)


def _replay(change: str) -> tuple[str, str, str | None]:
    """Classify the recorded log and fold it from `red`: (event kind, red kind, state reached)."""
    state = ModelLaneWorkState(
        workflow_id="replay-1",
        change=ModelLaneWorkChange(
            owner="OmniNode-ai", repo="omnimarket", pr_number=1, ticket="OMN-26043"
        ),
        state=EnumLaneWorkState.RED,
        seq=4,
        expected_head=HEAD,
        worker_lane="red-1",
    )
    event = _classifier().classify(
        raw_result=(FIXTURES / LOGS[change]).read_text(), state=state
    )
    assert event is not None
    driver = Driver("red")
    result = driver.handler.reduce(
        driver.state, event, "2026-11-28T02:00:00Z", driver.config
    )
    assert result.outcome == "transition", result.refusal
    return event.kind.value, str(event.red_kind), result.state.name


def test_6573_file_shape_ratchet_routes_to_fixing_not_diagnosing() -> None:
    assert _replay("6573") == ("log_read", "file_shape_ratchet", "fixing")


def test_7030_overlay_refusal_is_overlay_pr_needed_and_routes_to_help() -> None:
    assert _replay("7030-overlay") == ("overlay_pr_needed", "None", "help_requested")


def test_6802_document_invalid_stays_a_log_read_and_goes_to_diagnosing() -> None:
    # The refusal names a model field the change must carry, not an overlay-store pull request.
    state = ModelLaneWorkState(
        workflow_id="replay-1",
        change=ModelLaneWorkChange(
            owner="OmniNode-ai", repo="omnimarket", pr_number=1, ticket="OMN-26043"
        ),
        state=EnumLaneWorkState.RED,
        seq=4,
        expected_head=HEAD,
        worker_lane="red-1",
    )
    event = _classifier().classify(
        raw_result=(
            FIXTURES / "omnimarket_6802_overlay_main_load_job_140182703055.txt"
        ).read_text(),
        state=state,
    )
    assert event is not None
    assert (event.kind.value, event.red_kind) == ("log_read", "overlay_required")
    driver = Driver("red")
    result = driver.handler.reduce(
        driver.state, event, "2026-11-28T02:00:00Z", driver.config
    )
    assert result.state.name == "diagnosing"


def test_7030_dod_verify_is_classified_and_goes_to_diagnosing() -> None:
    assert _replay("7030-dod") == ("log_read", "dod_verify_step_failed", "diagnosing")


def test_the_overlay_help_request_names_the_remedy_and_the_origin() -> None:
    driver = Driver("red")
    event = _classifier().classify(
        raw_result=(FIXTURES / LOGS["7030-overlay"]).read_text(),
        state=ModelLaneWorkState(
            workflow_id="replay-1",
            change=ModelLaneWorkChange(
                owner="OmniNode-ai", repo="omnimarket", pr_number=1
            ),
            state=EnumLaneWorkState.RED,
            seq=1,
        ),
    )
    assert event is not None
    assert event.kind is EnumLaneWorkEvent.OVERLAY_PR_NEEDED
    assert any("requires the overlay" in line for line in event.evidence)
    result = driver.handler.reduce(
        driver.state, event, "2026-11-28T02:00:00Z", driver.config
    )
    intent = result.intents[0]
    assert intent.name == "help_request"
    assert intent.inputs["help_origin"] == "red"
    assert TIERS[0].name == intent.inputs["tier"]
