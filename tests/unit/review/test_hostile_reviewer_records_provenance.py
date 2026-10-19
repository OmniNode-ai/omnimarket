# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""AC3 (OMN-21189): a producer records the target snapshot and the served-model
fields on its emitted record. The hostile reviewer is the producer; its completed
event is the record until the review-record model lands."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from omnibase_core.models.dispatch.model_handler_output import ModelHandlerOutput

from omnimarket.inference.adapter_inference_bridge import (
    ModelInferenceAdapter,
    ModelInferenceJsonObjectResponseFormat,
)
from omnimarket.inference.protocol_config import ModelInferenceProtocolSelection
from omnimarket.models.model_inference_provenance import (
    UNKNOWN,
    ModelInferenceProvenance,
    ModelServedInferenceResult,
    ModelServedSettings,
    ModelServedUsage,
)
from omnimarket.models.model_review_target_snapshot import (
    EnumReviewSnapshotStatus,
    ModelReviewTargetSnapshot,
)
from omnimarket.nodes.node_github_diff_effect.handlers.handler_github_diff import (
    HandlerGithubDiffEffect,
)
from omnimarket.nodes.node_hostile_reviewer_orchestrator.handlers.handler_hostile_reviewer_orchestrator import (
    HandlerHostileReviewerOrchestrator,
)
from omnimarket.nodes.node_hostile_reviewer_orchestrator.model_config_loader import (
    load_fallback_route,
)
from omnimarket.nodes.node_hostile_reviewer_orchestrator.models.model_hostile_reviewer_start_command import (
    ModelHostileReviewerStartCommand,
)
from omnimarket.review.node_io import (
    ModelGithubDiffCommand,
    ModelGithubDiffResolvedEvent,
)

_DIFF = "diff --git a/foo.py b/foo.py\n+print('x')\n"
_SNAPSHOT = ModelReviewTargetSnapshot.established(
    head_sha="a" * 40, base_sha="b" * 40, diff_text=_DIFF
)
_SERVED = ModelInferenceProvenance(
    served_model="served-model-2026-10",
    settings=ModelServedSettings(temperature=0.2, max_tokens=2048),
    usage=ModelServedUsage(input_tokens=100, output_tokens=20, total_tokens=120),
)


class _SnapshotDiffEffect(HandlerGithubDiffEffect):
    def __init__(self, snapshot: ModelReviewTargetSnapshot | None) -> None:
        self._snapshot = snapshot

    async def handle(self, request: ModelGithubDiffCommand) -> ModelHandlerOutput[None]:
        event = ModelGithubDiffResolvedEvent(
            correlation_id=request.correlation_id,
            repo=request.repo,
            pr_number=request.pr_number,
            content=_DIFF,
            content_chars=len(_DIFF),
            snapshot=self._snapshot,
        )
        return ModelHandlerOutput.for_effect(
            input_envelope_id=uuid4(),
            correlation_id=request.correlation_id,
            handler_id="node_github_diff_effect",
            events=(event,),
        )


class _ServedAdapter(ModelInferenceAdapter):
    """Reports provenance for ``served`` routes, raises for the ``broken`` keys."""

    def __init__(self, broken: frozenset[str] = frozenset({"broken"})) -> None:
        self._broken = broken

    async def infer(
        self,
        model_key: str,
        system_prompt: str,
        user_prompt: str,
        timeout_seconds: float,
        temperature: float | None = None,
        response_format: ModelInferenceJsonObjectResponseFormat | None = None,
        protocol_selection: ModelInferenceProtocolSelection | None = None,
    ) -> str:
        return (
            await self.infer_with_provenance(
                model_key, system_prompt, user_prompt, timeout_seconds
            )
        ).text

    async def infer_with_provenance(
        self,
        model_key: str,
        system_prompt: str,
        user_prompt: str,
        timeout_seconds: float,
        temperature: float | None = None,
        response_format: ModelInferenceJsonObjectResponseFormat | None = None,
        protocol_selection: ModelInferenceProtocolSelection | None = None,
    ) -> ModelServedInferenceResult:
        if model_key in self._broken:
            raise RuntimeError("connection refused")
        if model_key == "silent":
            return ModelServedInferenceResult(text="[]")
        return ModelServedInferenceResult(text="[]", provenance=_SERVED)


def _command(
    models: list[str], *, diff_content: str | None = None
) -> ModelHostileReviewerStartCommand:
    return ModelHostileReviewerStartCommand(
        correlation_id=uuid4(),
        pr_number=None if diff_content else 42,
        repo=None if diff_content else "o/r",
        diff_content=diff_content,
        models=models,
        requested_at=datetime.now(tz=UTC),
    )


def _orchestrator(
    snapshot: ModelReviewTargetSnapshot | None,
    adapter: ModelInferenceAdapter | None = None,
) -> HandlerHostileReviewerOrchestrator:
    return HandlerHostileReviewerOrchestrator(
        inference_adapter=adapter or _ServedAdapter(),
        github_diff_effect=_SnapshotDiffEffect(snapshot),
    )


@pytest.mark.asyncio
async def test_record_carries_snapshot_and_served_model_per_route() -> None:
    event = await _orchestrator(_SNAPSHOT).handle(_command(["served", "silent"]))

    assert event.target_snapshot == _SNAPSHOT
    assert event.route_provenance["served"] == _SERVED
    # a route that reports nothing reads unknown, never the requested key
    silent = event.route_provenance["silent"]
    assert silent.served_model == UNKNOWN
    assert silent == ModelInferenceProvenance()


@pytest.mark.asyncio
async def test_failed_route_is_recorded_with_unknown_provenance() -> None:
    event = await _orchestrator(_SNAPSHOT).handle(_command(["served", "broken"]))

    assert event.models_failed == ["broken"]
    assert event.route_provenance["broken"] == ModelInferenceProvenance()
    assert event.route_provenance["served"] == _SERVED


@pytest.mark.asyncio
async def test_unavailable_snapshot_is_recorded_with_its_reason() -> None:
    unavailable = ModelReviewTargetSnapshot.unavailable("head moved between reads")

    event = await _orchestrator(unavailable).handle(_command(["served"]))

    assert event.target_snapshot is not None
    assert event.target_snapshot.status is EnumReviewSnapshotStatus.UNAVAILABLE
    assert event.target_snapshot.unavailable_reason == "head moved between reads"


@pytest.mark.asyncio
async def test_effect_that_returns_no_snapshot_reads_unavailable() -> None:
    event = await _orchestrator(None).handle(_command(["served"]))

    assert event.target_snapshot is not None
    assert event.target_snapshot.status is EnumReviewSnapshotStatus.UNAVAILABLE
    assert event.target_snapshot.unavailable_reason


@pytest.mark.asyncio
async def test_inline_diff_has_no_head_so_snapshot_is_unavailable() -> None:
    event = await _orchestrator(_SNAPSHOT).handle(
        _command(["served"], diff_content=_DIFF)
    )

    assert event.target_snapshot is not None
    assert event.target_snapshot.status is EnumReviewSnapshotStatus.UNAVAILABLE


@pytest.mark.asyncio
async def test_all_routes_failed_still_records_snapshot_and_routes() -> None:
    fallback = load_fallback_route()
    assert fallback is not None
    adapter = _ServedAdapter(broken=frozenset({"broken", fallback.key}))

    event = await _orchestrator(_SNAPSHOT, adapter).handle(_command(["broken"]))

    assert event.error_message is not None
    assert event.target_snapshot == _SNAPSHOT
    assert event.route_provenance == {
        "broken": ModelInferenceProvenance(),
        fallback.key: ModelInferenceProvenance(),
    }


@pytest.mark.asyncio
async def test_fallback_engine_records_its_served_provenance() -> None:
    fallback = load_fallback_route()
    assert fallback is not None

    event = await _orchestrator(_SNAPSHOT).handle(_command(["broken"]))

    assert event.error_message is None
    assert event.fallback_engine == fallback.key
    assert event.target_snapshot == _SNAPSHOT
    assert event.route_provenance == {
        "broken": ModelInferenceProvenance(),
        fallback.key: _SERVED,
    }
