# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Held asks keep visibly unverified candidates; only measured failures retry."""

from __future__ import annotations

import asyncio
import json
from typing import cast

import pytest
from pydantic import JsonValue

from omnimarket.nodes.node_linear_comments_sweep_orchestrator.handlers.handler_linear_comments_sweep import (
    HandlerLinearCommentsSweep,
)
from omnimarket.nodes.node_linear_comments_sweep_orchestrator.handlers.handler_sweep_bus import (
    HandlerSweepBus,
)
from omnimarket.nodes.node_linear_comments_sweep_orchestrator.models import (
    ModelSweepOverlay,
    ModelSweepPhaseReply,
    ModelSweepPhaseRequest,
)

from .helpers import (
    SELECTED,
    FixtureGateway,
    RealDecisions,
    draft_for,
    failed,
    item_of,
    ok,
    public_overlay,
    request,
)
from .test_phase_bus import Rpc, _phase, _terminal

TARGET = SELECTED[0]


def run_sweep(
    failure: str,
    *,
    retry_failure: str = "",
    retry_backend: str | None = "next-draft-backend",
    stage: str = "delegate",
) -> tuple[dict[str, JsonValue], FixtureGateway]:
    def script(phase: ModelSweepPhaseRequest) -> ModelSweepPhaseReply:
        if phase.label.startswith(f"{stage}:") and TARGET in phase.label:
            if phase.label.endswith(":transport-retry"):
                return (
                    failed(retry_failure)
                    if retry_failure
                    else FixtureGateway.default(phase)
                )
            return failed(failure)
        return FixtureGateway.default(phase)

    gateway = FixtureGateway(script)
    overlay = public_overlay().model_copy(update={"retry_backend_id": retry_backend})
    result = asyncio.run(
        HandlerLinearCommentsSweep(None, gateway, RealDecisions(), overlay).handle(
            request()
        )
    )
    writer = gateway.calls[-1].prompt
    payload = cast(
        dict[str, JsonValue],
        json.loads(writer.split(" payload ")[1].split(" metrics ")[0]),
    )
    assert result.accepted + result.held == result.needing
    assert result.held_drafts == payload["held"]
    return payload, gateway


@pytest.mark.parametrize(
    "failure",
    [
        "connection reset",
        "ConnectionRefusedError: backend unavailable",
        "delegate call timed out after 180 s",
        "delegate timed out: No service was listening on a topic",
        "DelegateNoConsumerRefusedError: absent_consumer",
    ],
)
def test_measured_transport_failure_retries_once_on_declared_backend(
    failure: str,
) -> None:
    payload, gateway = run_sweep(failure)
    calls = [
        c
        for c in gateway.calls
        if c.label.startswith("delegate:") and TARGET in c.label
    ]
    assert len(calls) == 2
    assert calls[1].label.endswith(":transport-retry")
    assert calls[1].backend_id == "next-draft-backend"
    assert calls[1].max_tokens == calls[0].max_tokens
    assert payload["held"] == []


@pytest.mark.parametrize(
    "failure",
    [
        "proven floor not met: connection reset",
        "TASK_MISMATCH: unsupported assertion",
        "permission denied",
        "invalid JSON",
        "unknown failure",
        "HTTP 401 unauthorized",
        "HTTP 429 rate limit",
        "timed out after HTTP 429 rate limit",
    ],
)
def test_other_failures_do_not_retry(failure: str) -> None:
    payload, gateway = run_sweep(failure)
    assert (
        len(
            [
                c
                for c in gateway.calls
                if c.label.startswith("delegate:") and TARGET in c.label
            ]
        )
        == 1
    )
    held = cast(list[dict[str, JsonValue]], payload["held"])
    assert held[0]["reason"] == failure


@pytest.mark.parametrize(
    ("failure", "expected_class"),
    [
        ("connection reset", "transport"),
        ("No service was listening on a topic", "no_consumer"),
        ("delegation timed out", "timeout"),
    ],
)
def test_exhausted_retry_stays_held_with_a_visible_fallback(
    failure: str, expected_class: str
) -> None:
    payload, gateway = run_sweep(failure, retry_failure=failure)
    held = cast(list[dict[str, JsonValue]], payload["held"])
    assert held[0]["reason_class"] == expected_class
    fallback = cast(dict[str, JsonValue], held[0]["fallback_draft"])
    assert fallback["label"] == "UNVERIFIED DRAFT"
    assert fallback["verified"] is False
    assert fallback["accepted"] is False
    assert fallback["comment_id"] == TARGET
    assert fallback["proposed_reply"] == ""
    assert fallback["open_points_for_operator"]
    assert (
        len(
            [
                c
                for c in gateway.calls
                if c.label.startswith("delegate:") and TARGET in c.label
            ]
        )
        == 2
    )
    assert not [
        c for c in gateway.calls if c.label.startswith("verify:") and TARGET in c.label
    ]
    assert "UNVERIFIED DRAFT" in gateway.calls[-1].prompt


def test_no_retry_target_keeps_failure_held_without_guessing_a_route() -> None:
    payload, gateway = run_sweep("connection reset", retry_backend=None)
    assert (
        len(
            [
                c
                for c in gateway.calls
                if c.label.startswith("delegate:") and TARGET in c.label
            ]
        )
        == 1
    )
    assert cast(list[dict[str, JsonValue]], payload["held"])[0]["fallback_draft"]


def test_verifier_failure_keeps_original_candidate_unverified_without_redrafting() -> (
    None
):
    payload, gateway = run_sweep("verifier timed out", stage="verify")
    held = cast(list[dict[str, JsonValue]], payload["held"])
    fallback = cast(dict[str, JsonValue], held[0]["fallback_draft"])
    assert (
        fallback["proposed_reply"]
        == draft_for(item_of(f"delegate:T:{TARGET}"))["proposed_reply"]
    )
    assert fallback["verified"] is False
    assert (
        len(
            [
                c
                for c in gateway.calls
                if c.label.startswith("delegate:") and TARGET in c.label
            ]
        )
        == 1
    )


def test_retry_backend_reaches_the_typed_delegation_command() -> None:
    rpc = Rpc(_terminal())
    phase = _phase().model_copy(update={"backend_id": "next-draft-backend"})
    asyncio.run(HandlerSweepBus(rpc=rpc).run(request(), phase))
    envelope = rpc.calls[0][1]
    assert (
        cast(dict[str, object], envelope["payload"])["backend_id"]
        == "next-draft-backend"
    )


def test_overlay_reads_the_declared_retry_backend() -> None:
    original = public_overlay()
    briefs = "\n".join(
        f"<!-- prompt:{name} -->\n{text}\n<!-- end:{name} -->"
        for name, text in original.templates.items()
    )
    parsed = ModelSweepOverlay.from_documents(
        {"values": original.values, "retry_backend_id": "next-draft-backend"}, briefs
    )
    assert parsed.retry_backend_id == "next-draft-backend"


@pytest.mark.parametrize("backend", ["", " bad", "bad ", "two backends", 1])
def test_invalid_retry_backend_is_refused(backend: object) -> None:
    from omnimarket.nodes.node_linear_comments_sweep_orchestrator.models import (
        ModelSweepOverlay,
    )

    original = public_overlay()
    with pytest.raises(ValueError, match="retry_backend_id"):
        ModelSweepOverlay.model_validate(
            {**original.model_dump(), "retry_backend_id": backend}
        )


@pytest.mark.parametrize("first_cut", [False, True])
def test_transport_and_truncation_share_a_two_attempt_budget(first_cut: bool) -> None:
    calls = 0

    def script(phase: ModelSweepPhaseRequest) -> ModelSweepPhaseReply:
        nonlocal calls
        if phase.label.startswith("delegate:") and TARGET in phase.label:
            calls += 1
            if (calls == 1) == first_cut:
                return ModelSweepPhaseReply(
                    ok=True, content='{"proposed_reply":', truncated=True
                )
            return failed("No service was listening on a topic")
        return FixtureGateway.default(phase)

    gateway = FixtureGateway(script)
    overlay = public_overlay().model_copy(
        update={"retry_backend_id": "next-draft-backend"}
    )
    result = asyncio.run(
        HandlerLinearCommentsSweep(None, gateway, RealDecisions(), overlay).handle(
            request()
        )
    )
    assert calls == 2
    assert result.held == 1
    assert result.held_drafts[0]["fallback_draft"]
    assert result.held_drafts[0]["reason_class"] == (
        "no_consumer" if first_cut else "truncated"
    )


def test_a_typed_rpc_timeout_gets_one_retry_and_can_recover() -> None:
    class TimeoutGateway(FixtureGateway):
        async def run(
            self, run: object, phase: ModelSweepPhaseRequest
        ) -> ModelSweepPhaseReply:
            if (
                phase.label.startswith("delegate:")
                and TARGET in phase.label
                and not phase.label.endswith(":transport-retry")
            ):
                self.calls.append(phase)
                raise TimeoutError("request-response expired")
            return await super().run(request(), phase)

    gateway = TimeoutGateway()
    overlay = public_overlay().model_copy(
        update={"retry_backend_id": "next-draft-backend"}
    )
    result = asyncio.run(
        HandlerLinearCommentsSweep(None, gateway, RealDecisions(), overlay).handle(
            request()
        )
    )
    assert result.held == 0
    assert (
        len(
            [
                c
                for c in gateway.calls
                if c.label.startswith("delegate:") and TARGET in c.label
            ]
        )
        == 2
    )


def test_a_retry_never_pins_the_backend_that_just_failed() -> None:
    def script(phase: ModelSweepPhaseRequest) -> ModelSweepPhaseReply:
        if phase.label.startswith("delegate:") and TARGET in phase.label:
            return ModelSweepPhaseReply(
                ok=False, failure="connection reset", backend_id="next-draft-backend"
            )
        return FixtureGateway.default(phase)

    gateway = FixtureGateway(script)
    overlay = public_overlay().model_copy(
        update={"retry_backend_id": "next-draft-backend"}
    )
    result = asyncio.run(
        HandlerLinearCommentsSweep(None, gateway, RealDecisions(), overlay).handle(
            request()
        )
    )
    assert result.held == 1
    assert (
        len(
            [
                c
                for c in gateway.calls
                if c.label.startswith("delegate:") and TARGET in c.label
            ]
        )
        == 1
    )


def test_a_source_refusal_keeps_the_candidate_out_of_accepted_replies() -> None:
    candidate = draft_for(item_of(f"delegate:T:{TARGET}"))
    candidate["comment_id"] = "another-comment"
    candidate["accepted"] = True
    candidate["verified"] = True
    candidate["status"] = "ready to post"

    def script(phase: ModelSweepPhaseRequest) -> ModelSweepPhaseReply:
        if phase.label.startswith("delegate:") and TARGET in phase.label:
            return ok(json.dumps(candidate))
        return FixtureGateway.default(phase)

    gateway = FixtureGateway(script)
    result = asyncio.run(
        HandlerLinearCommentsSweep(
            None, gateway, RealDecisions(), public_overlay()
        ).handle(request())
    )
    assert result.held == 1
    assert result.held_drafts[0]["reason_class"] == "unsourced"
    fallback = cast(dict[str, JsonValue], result.held_drafts[0]["fallback_draft"])
    assert fallback["verified"] is False
    assert fallback["accepted"] is False
    assert "status" not in fallback
    assert fallback["comment_id"] == TARGET
    assert not [
        c for c in gateway.calls if c.label.startswith("verify:") and TARGET in c.label
    ]
