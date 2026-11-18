# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""OMN-19998: the rule node names an Actions 5xx failure as runner-infra from the experiment's items."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from omnimarket.nodes.node_ci_red_kind_rule_compute.handlers.handler_ci_red_kind_rule import (
    HandlerCiRedKindRule,
)
from omnimarket.nodes.node_ci_red_kind_rule_compute.models.model_ci_red_kind_rule_answer import (
    ModelCiRedKindRuleAnswer,
)
from omnimarket.nodes.node_ci_red_kind_rule_compute.models.model_ci_red_kind_rule_overlay import (
    ModelCiRedKindRuleOverlay,
)
from omnimarket.nodes.node_ci_red_kind_rule_compute.models.model_ci_red_kind_rule_request import (
    ModelCiRedKindRuleRequest,
)

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[3]
FIXTURE = REPO_ROOT / "tests/fixtures/ci_red_kind_rule/exp_cired_6803_items.json"
OVERLAY_EXAMPLE = (
    REPO_ROOT
    / "src/omnimarket/nodes/node_ci_red_kind_rule_compute/overlay.example.yaml"
)
LOG_RULE_INDEX = 2
# The fixture keeps no check names: no rule reads one for these items (none of them is a name-rule kind).
CHECK_NAME = "Tests"


def _rows(group: str) -> list[dict]:
    rows: list[dict] = json.loads(FIXTURE.read_text(encoding="utf-8"))
    return [row for row in rows if row["group"] == group]


def _example_doc() -> dict:
    doc: dict = yaml.safe_load(OVERLAY_EXAMPLE.read_text(encoding="utf-8"))
    return doc


def _handler(rule_count: int | None = None) -> HandlerCiRedKindRule:
    doc = _example_doc()
    if rule_count is not None:
        doc["rules"] = doc["rules"][:rule_count]
    overlay = ModelCiRedKindRuleOverlay.model_validate(doc)
    return HandlerCiRedKindRule(load_overlay=lambda: overlay)


def _answer(handler: HandlerCiRedKindRule, row: dict) -> ModelCiRedKindRuleAnswer:
    return handler.handle(
        ModelCiRedKindRuleRequest(check_name=CHECK_NAME, log_excerpt=row["excerpt"])
    )


def test_the_24_consistent_runner_infra_items_are_named_runner_infra_by_rule_two() -> (
    None
):
    rows = _rows("runner_infra_consistent")
    assert len(rows) == 24
    handler = _handler()
    for row in rows:
        answer = _answer(handler, row)
        assert answer.matched is True, row["id"]
        assert answer.kind == "runner-infra", row["id"]
        assert answer.rule_index == LOG_RULE_INDEX, row["id"]
        assert answer.model_call_required is False, row["id"]


def test_the_3_items_whose_504_text_is_retry_text_or_inside_a_codeql_error_stay_unlabelled() -> (
    None
):
    rows = _rows("retry_or_codeql_text_only")
    assert len(rows) == 3
    handler = _handler()
    for row in rows:
        answer = _answer(handler, row)
        assert answer.matched is False, row["id"]
        assert answer.model_call_required is True, row["id"]


def test_the_other_unknown_reds_of_the_experiment_stay_unlabelled() -> None:
    rows = _rows("other_unknown_reds")
    assert len(rows) == 5
    handler = _handler()
    for row in rows:
        assert _answer(handler, row).matched is False, row["id"]


def test_the_3_items_the_experiment_hand_labelled_other_carry_the_same_error_line() -> (
    None
):
    # The experiment hand-labelled these three `other` and disclosed that as inconsistent: their excerpt holds the
    # same Actions error line as the 24 runner-infra items, so the rule names them runner-infra like the 24.
    rows = _rows("hand_label_inconsistent")
    assert len(rows) == 3
    handler = _handler()
    for row in rows:
        assert row["hand_label"] == "other"
        answer = _answer(handler, row)
        assert answer.kind == "runner-infra", row["id"]


def test_without_the_overlay_value_none_of_the_24_items_is_labelled() -> None:
    # The failing control: the two name rules alone (the overlay without its third rule) label none of the 24.
    rows = _rows("runner_infra_consistent")
    handler = _handler(rule_count=LOG_RULE_INDEX)
    answers = [_answer(handler, row) for row in rows]
    assert [answer.matched for answer in answers] == [False] * 24
    assert all(answer.model_call_required for answer in answers)


def test_an_injected_break_with_the_log_pattern_in_the_wrong_order_labels_none_of_the_24() -> (
    None
):
    doc = _example_doc()
    doc["rules"][LOG_RULE_INDEX]["log_pattern"] = (
        r"Unexpected HTTP response: 5\d\d\n##\[error\]"
    )
    overlay = ModelCiRedKindRuleOverlay.model_validate(doc)
    handler = HandlerCiRedKindRule(load_overlay=lambda: overlay)
    matched = [
        _answer(handler, row).matched for row in _rows("runner_infra_consistent")
    ]
    assert matched.count(True) == 0
