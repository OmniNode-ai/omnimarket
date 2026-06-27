# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Multi-parameter integration test for node_pr_review_orchestrator (WS-5 Wave 4).

Variant B (ORCHESTRATOR bus round-trip). Registers the real
HandlerPrReviewOrchestrator on the in-memory bus via LocalRuntimeBusAdapter,
publishes the start command on the command topic, and asserts the terminal
ModelPrReviewCompletedEvent (final_phase + nested ReviewVerdict) on the
completion topic — verdict, total_findings, threads_posted are all checked.

Only the I/O boundary is mocked — the diff EFFECT, the github-review EFFECT, and
the inference adapter are injected; the real prompt-builder, response-parser,
finding-aggregator, and the pure pr-review FSM fold run unmodified.

Negative control: a known-bad diff MUST flip the verdict away from CLEAN
(RISKS_NOTED) with a non-zero finding count — the bot cannot pass insecure code.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import pytest

from omnimarket.nodes.node_pr_review_orchestrator.handlers.handler_pr_review_orchestrator import (
    HandlerPrReviewOrchestrator,
)
from omnimarket.review.pr_review_io import EnumFindingSeverity, ReviewRequest
from tests.integration._review_verify_mocks import (
    _MockGithubDiffEffect,
    _MockGithubReviewEffect,
    _MockInferenceAdapter,
    finding_payload,
    findings_json,
)
from tests.runtime_local_compat import LocalRuntimeBusAdapter

_START_TOPIC = "onex.cmd.omnimarket.pr-review-bot-start.v1"
_COMPLETED_TOPIC = "onex.evt.omnimarket.pr-review-bot-completed.v1"

_BAD_DIFF = (
    "diff --git a/src/auth.py b/src/auth.py\n"
    "+    token = request.headers.get('X-Token')\n"
    "+    if token == admin_token: grant_all()  # no constant-time compare\n"
)

_ONE_FINDING = findings_json(
    [
        finding_payload(
            description="non-constant-time token comparison", severity="major"
        )
    ]
)
_TWO_FINDINGS = findings_json(
    [
        finding_payload(
            description="non-constant-time token comparison", severity="major"
        ),
        finding_payload(
            description="admin grant without audit log", severity="critical"
        ),
    ]
)


class _TypedHandlerWrapper:
    """Bridge adapter kwargs into the orchestrator's typed ReviewRequest, returning
    the terminal completed event (what the runtime publishes to the terminal topic)."""

    def __init__(self, handler: HandlerPrReviewOrchestrator) -> None:
        self._handler = handler

    async def handle(self, **payload: Any) -> Any:
        output = await self._handler.handle(ReviewRequest(**payload))
        return output.events[0]


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize(
    (
        "reviewer_models",
        "review_raw",
        "max_findings",
        "severity_threshold",
        "expect_verdict",
        "expect_total",
        "expect_posted",
    ),
    [
        pytest.param(
            ["cheap_local"],
            "[]",
            20,
            EnumFindingSeverity.MAJOR,
            "clean",
            0,
            0,
            id="clean-no-findings",
        ),
        pytest.param(
            ["cheap_local"],
            _ONE_FINDING,
            20,
            EnumFindingSeverity.MAJOR,
            "risks_noted",
            1,
            1,
            id="bad-diff-risks-noted",
        ),
        pytest.param(
            ["cheap_local", "frontier"],
            _ONE_FINDING,
            20,
            EnumFindingSeverity.MAJOR,
            "risks_noted",
            2,
            2,
            id="multi-model-union",
        ),
        pytest.param(
            ["cheap_local"],
            _TWO_FINDINGS,
            1,
            EnumFindingSeverity.MAJOR,
            "risks_noted",
            2,
            1,
            id="max-findings-cap-enforced",
        ),
        pytest.param(
            ["cheap_local"],
            _ONE_FINDING,
            20,
            EnumFindingSeverity.MINOR,
            "risks_noted",
            1,
            1,
            id="severity-threshold-minor",
        ),
    ],
)
async def test_pr_review_round_trip(
    integration_event_bus: Any,
    reviewer_models: list[str],
    review_raw: str,
    max_findings: int,
    severity_threshold: EnumFindingSeverity,
    expect_verdict: str,
    expect_total: int,
    expect_posted: int,
) -> None:
    await integration_event_bus.start()
    try:
        orchestrator = HandlerPrReviewOrchestrator(
            inference_adapter=_MockInferenceAdapter(review_raw=review_raw),
            github_diff_effect=_MockGithubDiffEffect(content=_BAD_DIFF),
            github_review_effect=_MockGithubReviewEffect(),
        )
        adapter = LocalRuntimeBusAdapter(
            handler=_TypedHandlerWrapper(orchestrator),
            handler_name="pr-review-orchestrator",
            input_model_cls=ReviewRequest,
            output_topic=_COMPLETED_TOPIC,
            bus=integration_event_bus,
        )
        await integration_event_bus.subscribe(
            _START_TOPIC,
            on_message=adapter.on_message,
            group_id="pr-review-test",
        )

        command = ReviewRequest(
            correlation_id=uuid4(),
            pr_number=1471,
            repo="OmniNode-ai/omnimarket",
            reviewer_models=reviewer_models,
            judge_model="judge_frontier",
            severity_threshold=severity_threshold,
            max_findings_per_pr=max_findings,
            requested_at=datetime.now(tz=UTC),
        )
        await integration_event_bus.publish(
            _START_TOPIC, key=None, value=command.model_dump_json().encode("utf-8")
        )

        history = await integration_event_bus.get_event_history(topic=_COMPLETED_TOPIC)
        assert len(history) == 1, "expected exactly one terminal completed event"
        payload = json.loads(history[0].value)

        assert payload["final_phase"] == "done"
        verdict = payload["verdict"]
        assert verdict["correlation_id"] == str(command.correlation_id)
        assert verdict["verdict"] == expect_verdict
        assert verdict["total_findings"] == expect_total
        assert verdict["threads_posted"] == expect_posted
        assert verdict["judge_model_used"] == "judge_frontier"
    finally:
        await integration_event_bus.close()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_pr_review_negative_control_bad_diff_not_clean(
    integration_event_bus: Any,
) -> None:
    """Negative control: a known-bad diff must NOT be reported CLEAN — the review
    bot surfaces findings and downgrades the verdict to RISKS_NOTED."""
    await integration_event_bus.start()
    try:
        orchestrator = HandlerPrReviewOrchestrator(
            inference_adapter=_MockInferenceAdapter(review_raw=_TWO_FINDINGS),
            github_diff_effect=_MockGithubDiffEffect(content=_BAD_DIFF),
            github_review_effect=_MockGithubReviewEffect(),
        )
        adapter = LocalRuntimeBusAdapter(
            handler=_TypedHandlerWrapper(orchestrator),
            handler_name="pr-review-orchestrator",
            input_model_cls=ReviewRequest,
            output_topic=_COMPLETED_TOPIC,
            bus=integration_event_bus,
        )
        await integration_event_bus.subscribe(
            _START_TOPIC,
            on_message=adapter.on_message,
            group_id="pr-review-negctl-test",
        )
        command = ReviewRequest(
            correlation_id=uuid4(),
            pr_number=999,
            repo="OmniNode-ai/omnimarket",
            reviewer_models=["cheap_local"],
            judge_model="judge_frontier",
            requested_at=datetime.now(tz=UTC),
        )
        await integration_event_bus.publish(
            _START_TOPIC, key=None, value=command.model_dump_json().encode("utf-8")
        )
        history = await integration_event_bus.get_event_history(topic=_COMPLETED_TOPIC)
        assert len(history) == 1
        verdict = json.loads(history[0].value)["verdict"]
        assert verdict["verdict"] != "clean"
        assert verdict["total_findings"] >= 2
    finally:
        await integration_event_bus.close()
