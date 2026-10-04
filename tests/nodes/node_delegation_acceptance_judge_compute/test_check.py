# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""check: a judge reply is accepted only when every item is answered once, soundly."""

import pytest

from omnimarket.nodes.node_delegation_acceptance_judge_compute.models.enum_acceptance_issue_code import (
    EnumAcceptanceIssueCode as Code,
)
from omnimarket.nodes.node_delegation_acceptance_judge_compute.models.enum_acceptance_operation import (
    EnumAcceptanceOperation,
)
from omnimarket.nodes.node_delegation_acceptance_judge_compute.models.enum_acceptance_run_status import (
    EnumAcceptanceRunStatus,
)
from omnimarket.nodes.node_delegation_acceptance_judge_compute.models.model_acceptance_judge_result import (
    ModelAcceptanceJudgeResult,
)
from tests.nodes.node_delegation_acceptance_judge_compute.builders import (
    entry,
    reply,
    run,
)

pytestmark = pytest.mark.unit


def _check(text: str, ids: tuple[str, ...] = ("a", "b")) -> ModelAcceptanceJudgeResult:
    return run(
        operation=EnumAcceptanceOperation.CHECK, reply_text=text, batch_item_ids=ids
    )


def _codes(result: ModelAcceptanceJudgeResult) -> list[tuple[Code, str]]:
    return [(i.code, i.item_id) for i in result.issues]


def test_check_accepts_a_sound_reply_in_batch_order() -> None:
    result = _check(
        reply(
            entry(
                "b",
                accept=False,
                quality=1,
                failure_class="fabrication",
                reason="invented id",
            ),
            entry("a"),
        )
    )
    assert result.status is EnumAcceptanceRunStatus.PASSED
    assert [v.item_id for v in result.verdicts] == ["a", "b"]
    assert result.verdicts[1].accept is False


def test_check_accepts_a_bare_list_and_one_code_fence() -> None:
    bare = _check(
        '[{"item_id":"a","accept":true,"quality":2,"failure_class":"none","reason":"ok"}]',
        ("a",),
    )
    assert bare.status is EnumAcceptanceRunStatus.PASSED
    fenced = _check("```json\n" + reply(entry("a")) + "\n```", ("a",))
    assert fenced.status is EnumAcceptanceRunStatus.PASSED


def test_check_refuses_prose_around_the_json() -> None:
    result = _check("Here are my judgments:\n" + reply(entry("a")), ("a",))
    assert result.status is EnumAcceptanceRunStatus.FAILED
    assert _codes(result) == [(Code.UNPARSEABLE_REPLY, "")]
    assert result.verdicts == ()


def test_check_refuses_a_missing_item() -> None:
    result = _check(reply(entry("a")))
    assert _codes(result) == [(Code.MISSING_ITEM, "b")]
    assert [v.item_id for v in result.verdicts] == ["a"]


def test_check_refuses_a_duplicated_item_and_keeps_neither_copy() -> None:
    result = _check(reply(entry("a"), entry("a"), entry("b")))
    assert _codes(result) == [(Code.DUPLICATE_ITEM, "a")]
    assert [v.item_id for v in result.verdicts] == ["b"]


def test_check_refuses_an_unknown_item() -> None:
    result = _check(reply(entry("a"), entry("b"), entry("zzz")))
    assert _codes(result) == [(Code.UNKNOWN_ITEM, "zzz")]
    assert result.status is EnumAcceptanceRunStatus.FAILED


def test_check_refuses_a_failure_class_outside_the_vocabulary() -> None:
    result = _check(
        reply(entry("a", accept=False, quality=0, failure_class="vibes"), entry("b"))
    )
    assert _codes(result) == [(Code.BAD_FAILURE_CLASS, "a")]


def test_check_refuses_an_accept_with_quality_below_two() -> None:
    result = _check(reply(entry("a", quality=1), entry("b")))
    assert _codes(result) == [(Code.ACCEPT_QUALITY_TOO_LOW, "a")]


def test_check_refuses_a_reject_without_a_failure_class() -> None:
    result = _check(
        reply(entry("a", accept=False, quality=1, failure_class="none"), entry("b"))
    )
    assert _codes(result) == [(Code.REJECT_WITHOUT_FAILURE_CLASS, "a")]


def test_check_refuses_an_accept_that_names_a_failure_class() -> None:
    result = _check(reply(entry("a", failure_class="incomplete"), entry("b")))
    assert _codes(result) == [(Code.ACCEPT_WITH_FAILURE_CLASS, "a")]


def test_check_refuses_a_reason_over_the_limit_and_an_empty_one() -> None:
    long_reason = "x" * 201
    result = _check(reply(entry("a", reason=long_reason), entry("b", reason="  ")))
    assert sorted(_codes(result), key=lambda c: c[1]) == [
        (Code.REASON_TOO_LONG, "a"),
        (Code.BAD_REASON, "b"),
    ]
    assert (
        _check(reply(entry("a", reason="x" * 200), entry("b"))).status
        is EnumAcceptanceRunStatus.PASSED
    )


def test_check_refuses_a_boolean_quality_and_a_string_accept() -> None:
    result = _check(reply(entry("a", quality=True), entry("b", accept="yes")))
    assert (Code.BAD_QUALITY, "a") in _codes(result)
    assert (Code.BAD_SHAPE, "b") in _codes(result)


def test_check_refuses_a_reply_of_the_wrong_shape() -> None:
    assert _codes(_check('{"answers": []}')) == [(Code.BAD_SHAPE, "")]
    assert _codes(_check("42")) == [(Code.BAD_SHAPE, "")]
