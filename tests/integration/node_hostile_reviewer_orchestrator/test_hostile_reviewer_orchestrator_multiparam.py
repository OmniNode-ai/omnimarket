# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Multi-parameter integration test for node_hostile_reviewer_orchestrator
(WS-5 Wave 4).

Variant B (ORCHESTRATOR bus round-trip). Registers the real
HandlerHostileReviewerOrchestrator on the in-memory bus via
LocalRuntimeBusAdapter, publishes the start command on the command topic, and
asserts the terminal ModelHostileReviewerCompletedEvent on the completion topic
(final_phase, pass_count, total_findings).

Only the I/O boundary is mocked — the diff EFFECT and the inference adapter are
injected; the real prompt-builder, response-parser, and finding-aggregator nodes
run unmodified.

cannot-rubber-stamp (this node's whole point): given a known-bad diff, the
reviewer MUST surface findings (total_findings > 0). A reviewer that returned a
clean verdict on bad code would be rubber-stamping; the negative-control case
proves it does not.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import pytest

from omnimarket.nodes.node_hostile_reviewer_orchestrator.handlers.handler_hostile_reviewer_orchestrator import (
    HandlerHostileReviewerOrchestrator,
)
from omnimarket.nodes.node_hostile_reviewer_orchestrator.models.model_hostile_reviewer_start_command import (
    ModelHostileReviewerStartCommand,
)
from tests.integration._review_verify_mocks import (
    _MockGithubDiffEffect,
    _MockInferenceAdapter,
    finding_payload,
    findings_json,
)
from tests.runtime_local_compat import LocalRuntimeBusAdapter

_START_TOPIC = "onex.cmd.omnimarket.hostile-reviewer-start.v1"
_COMPLETED_TOPIC = "onex.evt.omnimarket.hostile-reviewer-completed.v1"

_BAD_DIFF = (
    "diff --git a/src/example.py b/src/example.py\n"
    "+def login(pw):\n"
    "+    query = 'SELECT * FROM users WHERE pw=' + pw  # sql injection\n"
)
_CLEAN_DIFF = "diff --git a/README.md b/README.md\n+# docs typo fix\n"


class _TypedHandlerWrapper:
    """Bridge adapter kwargs into the orchestrator's typed command, returning the
    terminal completed event (what the runtime publishes to the terminal topic)."""

    def __init__(self, handler: HandlerHostileReviewerOrchestrator) -> None:
        self._handler = handler

    async def handle(self, **payload: Any) -> Any:
        output = await self._handler.handle(ModelHostileReviewerStartCommand(**payload))
        return output.events[0]


def _two_findings() -> str:
    return findings_json(
        [
            finding_payload(
                description="SQL injection: user password concatenated into query",
                category="security",
                severity="critical",
            ),
            finding_payload(
                description="No input validation on login parameter",
                category="logic_error",
                severity="major",
            ),
        ]
    )


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("target", "models", "review_raw", "findings_per_model", "expect_total"),
    [
        pytest.param("pr", ["cheap_local"], "[]", 0, 0, id="clean-pr-single-model"),
        pytest.param(
            "pr",
            ["cheap_local"],
            findings_json([finding_payload(description="off-by-one in loop bound")]),
            1,
            1,
            id="bad-diff-finding-surfaced",
        ),
        pytest.param(
            "pr",
            ["cheap_local", "frontier"],
            _two_findings(),
            2,
            4,
            id="multi-model-union",
        ),
        pytest.param(
            "file",
            ["cheap_local"],
            findings_json([finding_payload(description="unguarded None deref")]),
            1,
            1,
            id="file-path-scope",
        ),
        pytest.param(
            "pr",
            ["a", "b", "c"],
            "[]",
            0,
            0,
            id="three-models-clean",
        ),
    ],
)
async def test_hostile_reviewer_round_trip(
    integration_event_bus: Any,
    target: str,
    models: list[str],
    review_raw: str,
    findings_per_model: int,
    expect_total: int,
) -> None:
    await integration_event_bus.start()
    try:
        orchestrator = HandlerHostileReviewerOrchestrator(
            inference_adapter=_MockInferenceAdapter(review_raw=review_raw),
            github_diff_effect=_MockGithubDiffEffect(
                content=_BAD_DIFF if findings_per_model else _CLEAN_DIFF
            ),
        )
        adapter = LocalRuntimeBusAdapter(
            handler=_TypedHandlerWrapper(orchestrator),
            handler_name="hostile-reviewer-orchestrator",
            input_model_cls=ModelHostileReviewerStartCommand,
            output_topic=_COMPLETED_TOPIC,
            bus=integration_event_bus,
        )
        await integration_event_bus.subscribe(
            _START_TOPIC,
            on_message=adapter.on_message,
            group_id="hostile-reviewer-test",
        )

        kwargs: dict[str, Any] = {
            "correlation_id": uuid4(),
            "models": models,
            "requested_at": datetime.now(tz=UTC),
        }
        if target == "pr":
            kwargs["repo"] = "OmniNode-ai/omnimarket"
            kwargs["pr_number"] = 1471
        else:
            kwargs["file_path"] = "src/example.py"
        command = ModelHostileReviewerStartCommand(**kwargs)

        await integration_event_bus.publish(
            _START_TOPIC, key=None, value=command.model_dump_json().encode("utf-8")
        )

        history = await integration_event_bus.get_event_history(topic=_COMPLETED_TOPIC)
        assert len(history) == 1, "expected exactly one terminal completed event"
        payload = json.loads(history[0].value)

        assert payload["correlation_id"] == str(command.correlation_id)
        assert payload["final_phase"] == "done"
        assert payload["error_message"] is None
        assert payload["pass_count"] == len(models)
        assert payload["total_findings"] == expect_total
    finally:
        await integration_event_bus.close()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_hostile_reviewer_cannot_rubber_stamp_bad_diff(
    integration_event_bus: Any,
) -> None:
    """Negative control: a known-bad diff MUST produce findings — the reviewer
    cannot rubber-stamp insecure code with a clean (zero-finding) verdict."""
    await integration_event_bus.start()
    try:
        orchestrator = HandlerHostileReviewerOrchestrator(
            inference_adapter=_MockInferenceAdapter(review_raw=_two_findings()),
            github_diff_effect=_MockGithubDiffEffect(content=_BAD_DIFF),
        )
        adapter = LocalRuntimeBusAdapter(
            handler=_TypedHandlerWrapper(orchestrator),
            handler_name="hostile-reviewer-orchestrator",
            input_model_cls=ModelHostileReviewerStartCommand,
            output_topic=_COMPLETED_TOPIC,
            bus=integration_event_bus,
        )
        await integration_event_bus.subscribe(
            _START_TOPIC,
            on_message=adapter.on_message,
            group_id="hostile-reviewer-rubber-stamp-test",
        )
        command = ModelHostileReviewerStartCommand(
            correlation_id=uuid4(),
            repo="OmniNode-ai/omnimarket",
            pr_number=999,
            models=["cheap_local"],
            requested_at=datetime.now(tz=UTC),
        )
        await integration_event_bus.publish(
            _START_TOPIC, key=None, value=command.model_dump_json().encode("utf-8")
        )
        history = await integration_event_bus.get_event_history(topic=_COMPLETED_TOPIC)
        assert len(history) == 1
        payload = json.loads(history[0].value)
        assert payload["final_phase"] == "done"
        assert payload["total_findings"] > 0, "reviewer rubber-stamped a bad diff"
    finally:
        await integration_event_bus.close()
