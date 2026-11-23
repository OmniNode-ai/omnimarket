# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden and error chains for the pr-create refusal alert, over the three refusals of 2026-11-22 and the fourth (OMN-26433)."""

import json
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from omnimarket.nodes.node_pr_create_refusal_alert_compute.handlers import (
    HandlerPrCreateRefusalAlertCompute,
)
from omnimarket.nodes.node_pr_create_refusal_alert_compute.models import (
    ModelPrCreateRefusalAlertRequest,
    ModelPrCreateRefusalAlertResult,
)

pytestmark = pytest.mark.unit

_ROOT = Path(__file__).resolve().parents[1]
RECORDED = json.loads(
    (
        _ROOT
        / "tests/fixtures/work_ledger_reader/pr_duplicate_guard_claims_2026_11_22.json"
    ).read_text()
)
REFUSAL_ROWS = tuple(r["text"] for r in RECORDED["refusal_rows"])
#: A STATUS row of a fourth lane on 2026-11-22 whose cause is the missing reader overlay, and rows the check must not read.
OVERLAY_ROW = (
    "2026-11-22T03:10:10Z | STATUS | lane=a4closure2-0110 | principal=p | PR-NOT-OPENED: onex scm pr-create refused at "
    "PR-DUPLICATE-GUARD unchecked (reader pr-duplicate-guard-claims could not read the ledger on h105)"
)
UNRELATED = (
    "2026-11-22T03:00:00Z | STATUS | lane=other-0100 | principal=p | merged the pull request; CI green",
    "2026-11-22T03:01:00Z | CLAIM | lane=other-0100 | principal=p | exit 69 from an unrelated tool; PR-DUPLICATE-GUARD unchecked in a CLAIM is not read",
    "not a ledger row at all: pr-create hung",
)


def _handle(
    *rows: str, already: tuple[str, ...] = ()
) -> ModelPrCreateRefusalAlertResult:
    return HandlerPrCreateRefusalAlertCompute().handle(
        ModelPrCreateRefusalAlertRequest(
            window_start="2026-11-22T00:00:00Z",
            window_end="2026-11-22T04:00:00Z",
            rows=rows,
            already_alerted=already,
        )
    )


def test_golden_chain_the_three_recorded_refusals_raise_one_alert_per_cause() -> None:
    result = _handle(*REFUSAL_ROWS)

    assert result.rows_considered == 4
    by_cause = {a.cause: a for a in result.alerts}
    assert set(by_cause) == {"reader-exit-69", "pr-create-timeout"}
    assert by_cause["reader-exit-69"].first_lane == "undecl-0150"
    assert by_cause["reader-exit-69"].lanes_so_far == 2
    assert by_cause["pr-create-timeout"].first_lane == "noprdone2-0050"
    assert by_cause["pr-create-timeout"].lanes_so_far == 1
    assert [(c.cause, c.count) for c in result.classes] == [
        ("pr-create-timeout", 2),
        ("reader-exit-69", 2),
    ]


def test_golden_chain_the_first_lane_alone_raises_the_alert() -> None:
    first = _handle(REFUSAL_ROWS[0])
    assert [(a.cause, a.first_lane, a.lanes_so_far) for a in first.alerts] == [
        ("reader-exit-69", "undecl-0150", 1)
    ]


def test_golden_chain_a_cause_already_alerted_raises_no_second_alert() -> None:
    result = _handle(*REFUSAL_ROWS, already=("reader-exit-69",))
    assert [a.cause for a in result.alerts] == ["pr-create-timeout"]
    assert result.suppressed == ("reader-exit-69",)


def test_golden_chain_a_missing_overlay_is_its_own_cause_and_the_general_guard_row_is_named() -> (
    None
):
    result = _handle(OVERLAY_ROW)
    assert [a.cause for a in result.alerts] == ["guard-unchecked"]
    assert result.alerts[0].first_lane == "a4closure2-0110"


def test_golden_chain_rows_of_other_kinds_and_unrelated_rows_raise_nothing() -> None:
    """Positive control for the tests above: the same handler reads none of these."""
    result = _handle(*UNRELATED)
    assert result.alerts == ()
    assert result.classes == ()
    assert result.rows_considered == 1


def test_golden_chain_contract_declares_no_topic_of_its_own() -> None:
    contract = yaml.safe_load(
        (
            _ROOT
            / "src/omnimarket/nodes/node_pr_create_refusal_alert_compute/contract.yaml"
        ).read_text(encoding="utf-8")
    )
    assert contract["event_bus"] == {"subscribe_topics": [], "publish_topics": []}
    assert "terminal_event" not in contract


def test_error_chain_malformed_request_never_reaches_the_handler() -> None:
    with pytest.raises(ValidationError):
        ModelPrCreateRefusalAlertRequest.model_validate({"rows": [1]})
