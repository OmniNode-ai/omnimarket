# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The lane work event classifier's overlay document is a shared model its overlay can be pinned to (OMN-26707).

A node overlay is pinned to a merged revision that already holds its model, so the model lives
in the shared overlay documents package and merges ahead of the node that reads it.
"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from omnimarket.models.overlay_documents.model_lane_work_classify_overlay import (
    ModelLaneWorkClassifyOverlay,
)

_RULE: dict[str, Any] = {
    "pattern": "non-fast-forward",
    "event": "head_moved",
    "ignore_case": True,
}
_DOC: dict[str, Any] = {
    "schema_version": "lane_work_classify.v1",
    "ci_verdict_events": {"green": "check_green", "red": "check_red"},
    "rules": [
        _RULE,
        {
            "pattern": "lane cell",
            "event": "push_refused",
            "event_class": "guard_ledger_cell",
            "ignore_case": True,
        },
        {
            "pattern": "fails on dev itself",
            "event": "log_read",
            "ignore_case": True,
            "red_kind_check_name": "worker-log-read",
        },
    ],
}


def _with_rule(**changes: Any) -> dict[str, Any]:
    return {**_DOC, "rules": [{**_RULE, **changes}]}


def test_the_complete_document_loads_and_keeps_rule_order() -> None:
    doc = ModelLaneWorkClassifyOverlay.model_validate(_DOC)

    assert [r.event.value for r in doc.rules] == [
        "head_moved",
        "push_refused",
        "log_read",
    ]
    assert doc.rules[1].event_class == "guard_ledger_cell"
    assert doc.ci_verdict_events["red"].value == "check_red"


def test_a_document_without_rules_is_refused() -> None:
    with pytest.raises(ValidationError):
        ModelLaneWorkClassifyOverlay.model_validate({**_DOC, "rules": []})


def test_an_unknown_schema_version_is_refused() -> None:
    with pytest.raises(ValidationError):
        ModelLaneWorkClassifyOverlay.model_validate(
            {**_DOC, "schema_version": "lane_work_classify.v0"}
        )


def test_an_unknown_key_is_refused() -> None:
    with pytest.raises(ValidationError):
        ModelLaneWorkClassifyOverlay.model_validate({**_DOC, "extra_key": 1})


def test_a_pattern_that_is_not_a_regular_expression_is_refused() -> None:
    with pytest.raises(ValidationError, match="not a compilable"):
        ModelLaneWorkClassifyOverlay.model_validate(_with_rule(pattern="("))


def test_an_event_a_worker_text_cannot_name_is_refused() -> None:
    with pytest.raises(ValidationError, match="cannot be named from worker text"):
        ModelLaneWorkClassifyOverlay.model_validate(_with_rule(event="check_green"))


def test_an_event_class_the_event_does_not_carry_is_refused() -> None:
    with pytest.raises(ValidationError):
        ModelLaneWorkClassifyOverlay.model_validate(
            _with_rule(event="head_moved", event_class="hook")
        )


def test_a_red_kind_check_name_on_another_event_is_refused() -> None:
    with pytest.raises(ValidationError, match="belongs to a log_read rule"):
        ModelLaneWorkClassifyOverlay.model_validate(_with_rule(red_kind_check_name="x"))


def test_a_verdict_word_mapped_to_a_non_check_event_is_refused() -> None:
    with pytest.raises(ValidationError, match="only to check events"):
        ModelLaneWorkClassifyOverlay.model_validate(
            {**_DOC, "ci_verdict_events": {"green": "pushed"}}
        )


def test_the_model_is_frozen() -> None:
    doc = ModelLaneWorkClassifyOverlay.model_validate(_DOC)

    with pytest.raises(ValidationError):
        setattr(doc, "rules", ())  # noqa: B010
