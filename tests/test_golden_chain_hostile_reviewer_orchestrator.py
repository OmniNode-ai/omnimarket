# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden-chain tests for node_hostile_reviewer_orchestrator (OMN-13210 / B1).

Exercises the rebuilt canonical ORCHESTRATOR end-to-end with injected fakes for
the inference fan-out and the github-diff EFFECT, asserting:
  - the start command -> completed event chain preserves the
    ModelHostileReviewerCompletedEvent shape on the preserved topic,
  - the orchestrator emits via for_orchestrator(events=...) (events only),
  - per-model degradation does not abort the run,
  - all-models-fail still yields a DONE completed event (CLEAN, zero findings),
  - a fatal error yields a FAILED completed event with an error_message.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from omnibase_core.enums.enum_node_kind import EnumNodeKind
from omnibase_core.models.dispatch.model_handler_output import ModelHandlerOutput

from omnimarket.inference.adapter_inference_bridge import ModelInferenceAdapter
from omnimarket.nodes.node_github_diff_effect.handlers.handler_github_diff import (
    HandlerGithubDiffEffect,
)
from omnimarket.nodes.node_github_diff_effect.models.model_github_diff import (
    ModelGithubDiffCommand,
    ModelGithubDiffResolvedEvent,
)
from omnimarket.nodes.node_hostile_reviewer_orchestrator.handlers.handler_hostile_reviewer_orchestrator import (
    HandlerHostileReviewerOrchestrator,
)
from omnimarket.nodes.node_hostile_reviewer_orchestrator.models.model_hostile_reviewer_completed_event import (
    ModelHostileReviewerCompletedEvent,
)
from omnimarket.nodes.node_hostile_reviewer_orchestrator.models.model_hostile_reviewer_phase import (
    EnumHostileReviewerPhase,
)
from omnimarket.nodes.node_hostile_reviewer_orchestrator.models.model_hostile_reviewer_start_command import (
    ModelHostileReviewerStartCommand,
)


class _FakeInferenceAdapter(ModelInferenceAdapter):
    """Returns a canned raw response per model route key."""

    def __init__(self, responses: dict[str, str]) -> None:
        self._responses = responses

    async def infer(
        self,
        model_key: str,
        system_prompt: str,
        user_prompt: str,
        timeout_seconds: float,
        temperature: float | None = None,
    ) -> str:
        return self._responses.get(model_key, "[]")


class _FailInferenceAdapter(ModelInferenceAdapter):
    """Always raises — simulates a transport failure for every route."""

    async def infer(
        self,
        model_key: str,
        system_prompt: str,
        user_prompt: str,
        timeout_seconds: float,
        temperature: float | None = None,
    ) -> str:
        raise RuntimeError("connection refused")


class _StubGithubDiffEffect(HandlerGithubDiffEffect):
    """Returns a fixed diff without any network/secret resolution."""

    def __init__(self, content: str = "diff --git a/foo.py\n+print('x')") -> None:
        self._content = content

    async def handle(self, request: ModelGithubDiffCommand) -> ModelHandlerOutput[None]:
        event = ModelGithubDiffResolvedEvent(
            correlation_id=request.correlation_id,
            repo=request.repo,
            pr_number=request.pr_number,
            file_path=request.file_path,
            content=self._content,
            content_chars=len(self._content),
        )
        return ModelHandlerOutput.for_effect(
            input_envelope_id=uuid4(),
            correlation_id=request.correlation_id,
            handler_id="node_github_diff_effect",
            events=(event,),
        )


def _command(
    models: list[str] | None = None, pr_number: int | None = 42
) -> ModelHostileReviewerStartCommand:
    return ModelHostileReviewerStartCommand(
        correlation_id=uuid4(),
        pr_number=pr_number,
        repo="OmniNode-ai/test",
        models=models or ["review_primary"],
        dry_run=False,
        requested_at=datetime.now(tz=UTC),
    )


def _major_finding_json() -> str:
    return json.dumps(
        [
            {
                "category": "security",
                "severity": "major",
                "title": "XSS in template",
                "description": "Unescaped HTML output",
                "evidence": "line 10",
                "location": "template.html",
            }
        ]
    )


def _orchestrator(adapter: ModelInferenceAdapter) -> HandlerHostileReviewerOrchestrator:
    return HandlerHostileReviewerOrchestrator(
        inference_adapter=adapter,
        github_diff_effect=_StubGithubDiffEffect(),
    )


@pytest.mark.unit
@pytest.mark.asyncio
class TestHostileReviewerOrchestratorGoldenChain:
    async def test_happy_path_emits_completed_event(self) -> None:
        adapter = _FakeInferenceAdapter(
            {"review_primary": _major_finding_json(), "review_b": _major_finding_json()}
        )
        command = _command(models=["review_primary", "review_b"])

        output = await _orchestrator(adapter).handle(command)

        assert output.node_kind == EnumNodeKind.ORCHESTRATOR
        # ORCHESTRATOR output: events only, no result/projections.
        assert output.result is None
        assert output.projections == ()
        assert len(output.events) == 1
        completed = output.events[0]
        assert isinstance(completed, ModelHostileReviewerCompletedEvent)
        assert completed.correlation_id == command.correlation_id
        assert completed.final_phase == EnumHostileReviewerPhase.DONE
        assert completed.error_message is None
        assert completed.total_findings >= 1
        assert completed.pass_count == 2

    async def test_completed_event_shape_is_byte_stable(self) -> None:
        adapter = _FakeInferenceAdapter({"review_primary": _major_finding_json()})
        command = _command()

        output = await _orchestrator(adapter).handle(command)
        completed = output.events[0]
        payload = completed.model_dump(mode="json")

        # Preserved ModelHostileReviewerCompletedEvent shape (OMN-13210 replay).
        assert set(payload) == {
            "correlation_id",
            "final_phase",
            "started_at",
            "completed_at",
            "pass_count",
            "total_findings",
            "error_message",
        }
        assert payload["final_phase"] == "done"

    async def test_partial_failure_does_not_abort(self) -> None:
        # review_b returns empty findings; the run still completes DONE.
        adapter = _FakeInferenceAdapter({"review_primary": "[]"})
        command = _command(models=["review_primary", "review_b"])

        output = await _orchestrator(adapter).handle(command)
        completed = output.events[0]
        assert completed.final_phase == EnumHostileReviewerPhase.DONE
        assert completed.total_findings == 0

    async def test_all_models_fail_yields_clean_done(self) -> None:
        command = _command(models=["review_primary"])

        output = await _orchestrator(_FailInferenceAdapter()).handle(command)
        completed = output.events[0]
        # Per-model transport failures degrade gracefully -> DONE, zero findings.
        assert completed.final_phase == EnumHostileReviewerPhase.DONE
        assert completed.total_findings == 0
        assert completed.error_message is None

    async def test_fatal_error_yields_failed_event(self) -> None:
        # A github-diff effect that raises makes the whole run fail.
        class _BoomDiffEffect(HandlerGithubDiffEffect):
            async def handle(
                self, request: ModelGithubDiffCommand
            ) -> ModelHandlerOutput[None]:
                raise RuntimeError("diff resolution exploded")

        handler = HandlerHostileReviewerOrchestrator(
            inference_adapter=_FakeInferenceAdapter({"review_primary": "[]"}),
            github_diff_effect=_BoomDiffEffect(),
        )
        command = _command()

        output = await handler.handle(command)
        completed = output.events[0]
        assert completed.final_phase == EnumHostileReviewerPhase.FAILED
        assert completed.error_message is not None
        assert "exploded" in completed.error_message
