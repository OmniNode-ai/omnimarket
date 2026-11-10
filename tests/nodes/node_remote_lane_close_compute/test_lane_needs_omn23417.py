# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The closing row's typed needs cell and report text (OMN-23417, fix R1 of the diagnosed-red-stays-red plan).

Golden chain: a lane that ends blocked or partial and declares ``needs=<action>:<target>`` closes with that cell and its
report text on ``ledger_cells``, the cells the lane runner copies onto its TERMINAL. Error chain: a missing, malformed,
out-of-enum, fenced-in or contradictory cell never refuses the close; it is kept as ``needs=unparsed`` with the message.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from omnimarket.handlers.handler_packaged_contract_loader import (
    load_packaged_contract_section,
)
from omnimarket.models.remote_lane_close import (
    EnumLaneNeedsAction,
    ModelRemoteLaneNeedsPolicy,
    ModelRemoteLaneResult,
    ModelRemoteLaneResultRequest,
)
from omnimarket.nodes.node_remote_lane_close_compute.handlers.handler_remote_lane_result import (
    HandlerRemoteLaneResult,
)
from omnimarket.nodes.node_remote_lane_close_compute.overlay import (
    NODE_NAME,
    load_needs_policy,
)

pytestmark = pytest.mark.unit

DELEGATION = "DELEGATION delegated=0 reason=read-only-lane"
BLOCKED_RERUN = (
    "Blocked: the required check failed on a runner timeout, not on the change.\n"
    "BLOCKER: rerun the check on the exact head\n"
    "LANE_RESULT outcome=blocked needs=rerun:omnibase_core#2302\n" + DELEGATION
)


def close(
    message: str | None,
    exit_code: int = 0,
    handler: HandlerRemoteLaneResult | None = None,
) -> ModelRemoteLaneResult:
    return (handler or HandlerRemoteLaneResult()).handle(
        ModelRemoteLaneResultRequest(engine_exit_code=exit_code, final_message=message)
    )


def test_golden_a_blocked_lane_naming_a_rerun_closes_with_the_typed_cell_and_its_report() -> (
    None
):
    result = close(BLOCKED_RERUN)
    assert result.outcome == "blocked"
    assert result.needs is not None
    assert result.needs.action is EnumLaneNeedsAction.RERUN
    assert result.needs.target == "omnibase_core#2302"
    assert result.ledger_cells["needs"] == "rerun:omnibase_core#2302"
    report = result.ledger_cells["report"]
    assert report == result.report_text
    assert report.startswith("Blocked: the required check failed on a runner timeout")
    assert "DELEGATION" not in report
    assert "\n" not in report


def test_golden_a_partial_json_report_carries_its_cell() -> None:
    message = (
        '{"outcome": "partial", "needs": "update-branch:omnimarket#5001"}\n'
        + DELEGATION
    )
    result = close(message)
    assert result.outcome == "partial"
    assert result.ledger_cells["needs"] == "update-branch:omnimarket#5001"


@pytest.mark.parametrize(
    "action", [action.value for action in EnumLaneNeedsAction], ids=str
)
def test_golden_every_action_of_the_enum_parses(action: str) -> None:
    result = close(
        f"LANE_RESULT outcome=blocked needs={action}:OMN-23417\n{DELEGATION}"
    )
    assert result.ledger_cells["needs"] == f"{action}:OMN-23417"


def test_golden_a_done_lane_with_no_cell_writes_none_but_keeps_its_report() -> None:
    result = close(
        f"Opened the pull request.\nLANE_RESULT outcome=handed-off\n{DELEGATION}"
    )
    assert result.outcome == "handed-off"
    assert result.needs is None
    assert "needs" not in result.ledger_cells
    assert (
        result.ledger_cells["report"]
        == "Opened the pull request. LANE_RESULT outcome=handed-off"
    )


def test_error_a_blocked_lane_with_no_cell_is_kept_as_unparsed_with_its_message() -> (
    None
):
    result = close(
        f"I could not push; please rerun the check.\nLANE_RESULT outcome=blocked\n{DELEGATION}"
    )
    assert result.outcome == "blocked"
    assert result.ledger_cells["needs"] == "unparsed"
    assert "please rerun the check" in result.ledger_cells["report"]


@pytest.mark.parametrize(
    "cell",
    ["bogus:x", "rerun", "rerun:", "rerun:bad!target", "rerun:" + "a" * 130, ":x"],
)
def test_error_a_malformed_or_out_of_enum_cell_is_unparsed_and_never_refused(
    cell: str,
) -> None:
    result = close(f"LANE_RESULT outcome=blocked needs={cell}\n{DELEGATION}")
    assert result.outcome == "blocked"
    assert result.problem is None
    assert result.ledger_cells["needs"] == "unparsed"
    assert result.needs is not None
    assert result.needs.raw == cell[:200]


def test_error_two_different_cells_are_unparsed() -> None:
    result = close(
        "LANE_RESULT outcome=blocked needs=rerun:a#1\n"
        "TERMINAL outcome=blocked needs=close:a#1\n" + DELEGATION
    )
    assert result.ledger_cells["needs"] == "unparsed"


def test_error_a_cell_inside_a_code_fence_is_an_example_not_a_declaration() -> None:
    result = close(
        "```\nLANE_RESULT outcome=blocked needs=rerun:a#1\n```\n"
        f"LANE_RESULT outcome=blocked\n{DELEGATION}"
    )
    assert result.ledger_cells["needs"] == "unparsed"


def test_error_an_engine_failure_keeps_a_declared_cell_and_the_failed_outcome() -> None:
    result = close(BLOCKED_RERUN, exit_code=1)
    assert result.outcome == "failed"
    assert result.ledger_cells["needs"] == "rerun:omnibase_core#2302"


def test_error_a_report_with_a_cell_separator_or_newlines_is_one_safe_cell_value() -> (
    None
):
    result = close(f"a | b\n\n  c\r\nLANE_RESULT outcome=done\n{DELEGATION}")
    assert " | " not in result.ledger_cells["report"]
    assert "\n" not in result.ledger_cells["report"]
    assert result.ledger_cells["report"].startswith("a / b c")


def test_the_report_is_cut_to_the_policy_bound() -> None:
    policy = load_needs_policy().model_copy(update={"report_chars": 20})
    result = close(
        "x" * 100 + f"\nLANE_RESULT outcome=done\n{DELEGATION}",
        handler=HandlerRemoteLaneResult(policy),
    )
    assert result.ledger_cells["report"] == "x" * 20


def test_a_policy_that_narrows_the_actions_makes_the_dropped_action_unparsed() -> None:
    policy = load_needs_policy().model_copy(
        update={"actions": (EnumLaneNeedsAction.CLOSE,)}
    )
    result = close(BLOCKED_RERUN, handler=HandlerRemoteLaneResult(policy))
    assert result.ledger_cells["needs"] == "unparsed"


def test_the_contract_actions_are_exactly_the_enum() -> None:
    config = load_packaged_contract_section(NODE_NAME, "config")
    assert isinstance(config, dict)
    assert set(config["needs"]["actions"]) == {a.value for a in EnumLaneNeedsAction}


def test_the_overlay_example_is_a_valid_policy_equal_to_the_contract_defaults() -> None:
    example = Path(__file__).resolve().parents[3] / (
        "src/omnimarket/nodes/node_remote_lane_close_compute/overlay.example.yaml"
    )
    policy = ModelRemoteLaneNeedsPolicy.model_validate(
        yaml.safe_load(example.read_text(encoding="utf-8"))
    )
    assert policy == load_needs_policy()


def test_error_an_overlay_naming_an_action_outside_the_enum_refuses_composition() -> (
    None
):
    with pytest.raises(ValueError, match="actions"):
        ModelRemoteLaneNeedsPolicy.model_validate(
            {
                "actions": ["rerun", "merge"],
                "target_pattern": "x",
                "report_chars": 5,
                "cell_required_outcomes": [],
            }
        )
