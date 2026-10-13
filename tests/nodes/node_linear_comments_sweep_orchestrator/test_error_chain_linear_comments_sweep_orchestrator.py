# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Error chain: every way a sweep can lose an item or the window ends loud, never as a silent gap."""

from __future__ import annotations

import asyncio
import json
from typing import cast

import pytest
from pydantic import JsonValue, ValidationError

from omnimarket.nodes.node_linear_comments_sweep_orchestrator.handlers.handler_linear_comments_sweep import (
    HandlerLinearCommentsSweep,
    parse_draft,
)
from omnimarket.nodes.node_linear_comments_sweep_orchestrator.models import (
    ModelLinearCommentsSweepResult,
    ModelSweepPhaseReply,
    ModelSweepPhaseRequest,
)

from .helpers import (
    ITEMS,
    SELECTED,
    FixtureGateway,
    RealDecisions,
    collected_content,
    draft_for,
    failed,
    item_of,
    ok,
    public_overlay,
    request,
)

TARGET = SELECTED[0]


def _run(
    script: FixtureGateway | None = None,
) -> tuple[ModelLinearCommentsSweepResult, FixtureGateway]:
    gateway = script or FixtureGateway()
    result = asyncio.run(
        HandlerLinearCommentsSweep(
            None, gateway, RealDecisions(), public_overlay()
        ).handle(request())
    )
    return result, gateway


def _override(
    label_prefix: str, reply: ModelSweepPhaseReply, only: str | None = None
) -> FixtureGateway:
    def script(phase: ModelSweepPhaseRequest) -> ModelSweepPhaseReply:
        if phase.label.startswith(label_prefix) and (
            only is None or f":{only}" in phase.label
        ):
            return reply
        return FixtureGateway.default(phase)

    return FixtureGateway(script)


def _held_classes(result: ModelLinearCommentsSweepResult) -> dict[str, int]:
    return {k: v for k, v in result.held_by_class.items() if v}


def test_a_failed_collector_stops_the_sweep() -> None:
    gateway = _override("collect", failed("agent timed out"))
    with pytest.raises(ValueError, match="collect phase failed: agent timed out"):
        _run(gateway)
    assert gateway.labels() == ["collect:linear"]


def test_a_collector_without_a_positive_control_is_a_tooling_failure() -> None:
    gateway = _override("collect", ok(collected_content(control="")))
    with pytest.raises(ValueError, match="positive control"):
        _run(gateway)


def test_a_collector_off_its_schema_stops_the_sweep() -> None:
    gateway = _override("collect", ok(json.dumps({"items": "none"})))
    with pytest.raises(ValueError, match="off its schema"):
        _run(gateway)


def test_a_failed_delegation_is_held_as_transport_and_the_rest_continue() -> None:
    gateway = _override("delegate:", failed("connection reset"), only=TARGET)
    result, gateway = _run(gateway)
    assert result.accepted == len(SELECTED) - 1
    assert result.held == 1
    assert _held_classes(result) == {"transport": 1}
    assert not [c for c in gateway.labels() if c.startswith("verify:") and TARGET in c]


def test_a_floor_refusal_is_classified_from_the_delegation_failure() -> None:
    gateway = _override(
        "delegate:", failed("proven floor not met: the lab refused"), only=TARGET
    )
    result, _ = _run(gateway)
    assert _held_classes(result) == {"floor_refused": 1}


def test_a_cut_off_response_is_retried_once_with_the_larger_limit() -> None:
    cut = ModelSweepPhaseReply(
        ok=True,
        content='{"ticket": "OMN-1", "proposed_reply": "Ack',
        finish_reason="length",
        truncated=True,
    )
    calls = {"n": 0}

    def script(phase: ModelSweepPhaseRequest) -> ModelSweepPhaseReply:
        if phase.label.startswith("delegate:") and TARGET in phase.label:
            calls["n"] += 1
            if not phase.label.endswith(":retry"):
                return cut
        return FixtureGateway.default(phase)

    result, gateway = _run(FixtureGateway(script))
    assert result.accepted == len(SELECTED)
    assert result.held == 0
    retry = next(c for c in gateway.calls if c.label.endswith(":retry"))
    assert retry.max_tokens == 16384
    assert calls["n"] == 2


def test_two_cut_off_responses_are_held_as_truncated_and_never_verified() -> None:
    cut = ModelSweepPhaseReply(ok=True, content='{"ticket": "OMN-1"', truncated=True)
    gateway = _override("delegate:", cut, only=TARGET)
    result, gateway = _run(gateway)
    assert _held_classes(result) == {"truncated": 1}
    assert not [c for c in gateway.labels() if c.startswith("verify:") and TARGET in c]


def test_an_unparseable_draft_is_held_with_its_reason() -> None:
    gateway = _override("delegate:", ok("not json at all"), only=TARGET)
    result, _ = _run(gateway)
    assert result.held == 1
    assert result.accepted == len(SELECTED) - 1


def test_a_draft_that_cites_outside_its_sources_never_reaches_the_verifier() -> None:
    foreign = dict(draft_for(item_of(f"delegate:T:{TARGET}")))
    foreign["proposed_reply"] = (
        "See comment 11111111-2222-4333-8444-555555555555 for the details."
    )
    gateway = _override("delegate:", ok(json.dumps(foreign)), only=TARGET)
    result, gateway = _run(gateway)
    assert _held_classes(result) == {"unsourced": 1}
    assert not [c for c in gateway.labels() if c.startswith("verify:") and TARGET in c]


def test_a_draft_answering_another_comment_is_refused_before_verification() -> None:
    other = dict(draft_for(item_of(f"delegate:T:{TARGET}")))
    other["comment_id"] = "someone-else"
    gateway = _override("delegate:", ok(json.dumps(other)), only=TARGET)
    result, _ = _run(gateway)
    assert _held_classes(result) == {"unsourced": 1}


def test_a_verifier_rejection_is_held_with_the_verifiers_own_reason() -> None:
    reject = ok(
        json.dumps(
            {
                "accepted": False,
                "rejection_reason": "claims a date the facts do not give",
                "draft": draft_for(item_of(f"delegate:T:{TARGET}")),
            }
        ),
        "opus",
    )
    result, _ = _run(_override("verify:", reject, only=TARGET))
    assert result.accepted == len(SELECTED) - 1
    assert _held_classes(result) == {"unsourced": 1}


def test_a_dead_verifier_loses_only_its_item_and_says_where() -> None:
    result, _ = _run(_override("verify:", failed("agent died"), only=TARGET))
    assert result.accepted == len(SELECTED) - 1
    assert result.held == 1


def test_a_window_that_needed_replies_and_accepted_none_is_degraded() -> None:
    result, _ = _run(_override("delegate:", failed("connection reset")))
    assert result.needing == len(SELECTED)
    assert result.accepted == 0
    assert result.held == len(SELECTED)
    assert result.degraded is True


def test_a_window_with_nothing_to_answer_is_not_degraded() -> None:
    quiet = [dict(i, needs_reply=False) for i in ITEMS]
    gateway = _override(
        "collect", ok(json.dumps({"items": quiet, "positive_control": "control"}))
    )
    result, gateway = _run(gateway)
    assert result.needing == 0
    assert result.degraded is False
    assert gateway.labels() == ["collect:linear", "write:linear-file"]


def test_a_writer_that_wrote_another_path_is_refused() -> None:
    wrong = ok(
        json.dumps(
            {
                "report_path": "elsewhere.md",
                "metrics_path": "metrics/linear-comments.jsonl",
                "rendered_by": "fallback",
                "metrics_rows": 0,
            }
        )
    )
    with pytest.raises(ValueError, match=r"wrote elsewhere\.md"):
        _run(_override("write:", wrong))


def test_a_failed_writer_stops_the_sweep() -> None:
    with pytest.raises(ValueError, match="write phase failed: disk full"):
        _run(_override("write:", failed("disk full")))


def test_a_refused_decision_ends_the_sweep() -> None:
    class Refusing(RealDecisions):
        async def decide(
            self, run: object, request: dict[str, JsonValue]
        ) -> dict[str, JsonValue]:
            raise TimeoutError("decision node did not answer")

    with pytest.raises(TimeoutError, match="did not answer"):
        asyncio.run(
            HandlerLinearCommentsSweep(
                None,
                FixtureGateway(),
                cast(RealDecisions, Refusing()),
                public_overlay(),
            ).handle(request())
        )


@pytest.mark.parametrize(
    "args",
    [
        {"since": ""},
        {"since": "2026-09-14"},
        {"since": "2026-09-14T10:40:31+00:00"},
        {"facts": ""},
        {"task_type": "agent_delegation"},
        {"unexpected": 1},
    ],
)
def test_a_request_without_an_exact_cutoff_or_facts_is_refused(
    args: dict[str, JsonValue],
) -> None:
    with pytest.raises(ValidationError):
        request(args)


def test_a_fenced_draft_is_unwrapped_and_nothing_else_is_edited() -> None:
    assert parse_draft('```json\n{"a": 1}\n```') == {"a": 1}
    assert parse_draft('{"a": 1}') == {"a": 1}
    assert parse_draft("[1]") is None
    assert parse_draft("") is None
