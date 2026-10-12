# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Shared fixtures: the public test overlay, recorded gateways, a request builder."""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from pathlib import Path
from typing import cast

from pydantic import JsonValue

from omnimarket.nodes.node_linear_comments_decision_compute.handlers.handler_linear_comments_decision import (
    HandlerLinearCommentsDecision,
)
from omnimarket.nodes.node_linear_comments_decision_compute.models.model_linear_comments_decision import (
    ModelLinearCommentsDecisionRequest,
)
from omnimarket.nodes.node_linear_comments_sweep_orchestrator.handlers.handler_linear_comments_sweep import (
    OVERLAY_ENV,
    load_sweep_overlay,
)
from omnimarket.nodes.node_linear_comments_sweep_orchestrator.models import (
    ModelLinearCommentsSweepRequest,
    ModelSweepOverlay,
    ModelSweepPhaseReply,
    ModelSweepPhaseRequest,
)

NODE = "node_linear_comments_sweep_orchestrator"
ROOT = Path(__file__).resolve().parents[3]
OVERLAY_ROOT = Path(__file__).parent / "fixtures" / "overlay"
OVERLAY_FILE = OVERLAY_ROOT / NODE / "overlay.yaml"
FIXTURE = json.loads(
    (ROOT / "tests/fixtures/linear_comments_hourly_parity.json").read_text()
)
ITEMS: list[dict[str, JsonValue]] = FIXTURE["cases"]["items"]
SELECTED: list[str] = FIXTURE["expected"]["select"]
SINCE = "2026-09-14T10:40:31Z"
CONTROL = "comment bf9a438b on OMN-18268 sits just before the cutoff"


def public_overlay() -> ModelSweepOverlay:
    """Load the public test overlay through the production loader."""
    previous = os.environ.get(OVERLAY_ENV)
    os.environ[OVERLAY_ENV] = str(OVERLAY_FILE)
    try:
        return load_sweep_overlay()
    finally:
        if previous is None:
            del os.environ[OVERLAY_ENV]
        else:
            os.environ[OVERLAY_ENV] = previous


def request(
    args: dict[str, JsonValue] | None = None,
) -> ModelLinearCommentsSweepRequest:
    return ModelLinearCommentsSweepRequest.model_validate(
        {
            "correlation_id": "12345678-1234-5678-1234-567812345678",
            "emitted_at": "2026-10-11T11:00:00Z",
            "tenant_id": "tenant-a",
            "since": SINCE,
            "facts": "FACT-1: the sweep is read-only on Linear.",
            **(args or {}),
        }
    )


def collected_content(control: str = CONTROL) -> str:
    return json.dumps({"items": ITEMS, "positive_control": control})


def draft_for(item: dict[str, JsonValue]) -> dict[str, JsonValue]:
    return {
        "ticket": item["ticket"],
        "comment_id": item["comment_id"],
        "their_summary": "they ask",
        "proposed_reply": "Acknowledged.",
        "facts_cited": ["FACT-1"],
        "open_points_for_operator": [],
        "confidence": "high",
    }


def ok(content: str, model: str = "qwen") -> ModelSweepPhaseReply:
    return ModelSweepPhaseReply(
        ok=True,
        content=content,
        model_used=model,
        prompt_tokens=10,
        completion_tokens=20,
        total_tokens=30,
        latency_ms=400,
    )


def failed(reason: str) -> ModelSweepPhaseReply:
    return ModelSweepPhaseReply(ok=False, failure=reason)


def item_of(label: str) -> dict[str, JsonValue]:
    """The collected item a delegate or verify label names (ticket:comment_id[:retry])."""
    comment_id = label.split(":")[2]
    return next(i for i in ITEMS if i["comment_id"] == comment_id)


Script = Callable[[ModelSweepPhaseRequest], ModelSweepPhaseReply]


class FixtureGateway:
    """Recorded model answers; exercises orchestration without any model."""

    def __init__(self, script: Script | None = None) -> None:
        self.script = script
        self.calls: list[ModelSweepPhaseRequest] = []

    async def run(
        self, run: ModelLinearCommentsSweepRequest, phase: ModelSweepPhaseRequest
    ) -> ModelSweepPhaseReply:
        self.calls.append(phase)
        if self.script is not None:
            return self.script(phase)
        return self.default(phase)

    @staticmethod
    def default(phase: ModelSweepPhaseRequest) -> ModelSweepPhaseReply:
        if phase.label == "collect:linear":
            return ok(collected_content(), "sonnet")
        if phase.label.startswith("delegate:"):
            return ok(json.dumps(draft_for(item_of(phase.label))))
        if phase.label.startswith("verify:"):
            return ok(
                json.dumps(
                    {"accepted": True, "draft": draft_for(item_of(phase.label))}
                ),
                "opus",
            )
        return ok(
            json.dumps(
                {
                    "report_path": "drafts/linear-comment-replies-2026-09-14T104031Z.md",
                    "metrics_path": "metrics/linear-comments.jsonl",
                    "rendered_by": "delegated",
                    "metrics_rows": len(SELECTED),
                }
            ),
            "sonnet",
        )

    def labels(self) -> list[str]:
        return [c.label for c in self.calls]


class RealDecisions:
    """The decision compute node's own handler answering in place of the bus."""

    def __init__(self) -> None:
        self.handler = HandlerLinearCommentsDecision()
        self.kinds: list[str] = []

    async def decide(
        self, run: ModelLinearCommentsSweepRequest, request: dict[str, JsonValue]
    ) -> dict[str, JsonValue]:
        self.kinds.append(str(request["kind"]))
        result = self.handler.handle(
            ModelLinearCommentsDecisionRequest.model_validate(request)
        )
        return cast(dict[str, JsonValue], result.model_dump(mode="json"))
