# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Handler for node_adversarial_pipeline_orchestrator [OMN-12215].

This orchestrator owns stage ordering and gate semantics. The concrete design,
review, and ticket effects are injected native adapters; the handler does not
call other node handlers directly or dispatch background agents itself.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Protocol

from omnimarket.nodes.node_adversarial_pipeline_orchestrator.models.model_adversarial_pipeline_request import (
    ModelAdversarialPipelineRequest,
    ModelAdversarialPipelineResult,
)


class ProtocolDesignToPlanAdapter(Protocol):
    """Adapter boundary for Stage 1 plan generation."""

    def create_plan(self, payload: dict[str, Any]) -> Mapping[str, Any]: ...


class ProtocolHostileReviewAdapter(Protocol):
    """Adapter boundary for Stage 2 adversarial review."""

    def review_plan(self, payload: dict[str, Any]) -> Mapping[str, Any]: ...


class ProtocolPlanToTicketsAdapter(Protocol):
    """Adapter boundary for Stage 3 ticket creation."""

    def create_tickets(self, payload: dict[str, Any]) -> Mapping[str, Any]: ...


class HandlerAdversarialPipelineOrchestrator:
    """Run design -> review gate -> ticket creation through native adapters."""

    def __init__(
        self,
        design_adapter: ProtocolDesignToPlanAdapter | None = None,
        review_adapter: ProtocolHostileReviewAdapter | None = None,
        ticket_adapter: ProtocolPlanToTicketsAdapter | None = None,
    ) -> None:
        self._design_adapter = design_adapter
        self._review_adapter = review_adapter
        self._ticket_adapter = ticket_adapter

    def handle(
        self, request: ModelAdversarialPipelineRequest
    ) -> ModelAdversarialPipelineResult:
        plan_path = request.plan_path
        stage_reached = 1

        if not plan_path:
            if self._design_adapter is None:
                raise RuntimeError("design adapter required when plan_path is absent")
            design_result = self._design_adapter.create_plan(
                {
                    "topic": request.topic,
                    "linear_project": request.linear_project,
                    "no_launch": request.no_launch,
                }
            )
            plan_path = _required_str(design_result, "plan_path", "design")

        if self._review_adapter is None:
            raise RuntimeError("review adapter required for adversarial pipeline")
        stage_reached = 2
        review_result = self._review_adapter.review_plan(
            {"topic": request.topic, "plan_path": plan_path}
        )
        findings_count = int(review_result.get("findings_count", 0) or 0)
        findings_summary = str(review_result.get("findings_summary") or "")
        gate_passed = findings_count >= request.min_findings_gate

        if not gate_passed:
            return ModelAdversarialPipelineResult(
                plan_path=plan_path,
                findings_count=findings_count,
                findings_summary=findings_summary,
                gate_passed=False,
                dry_run=request.dry_run,
                stage_reached=stage_reached,
            )

        stage_reached = 3
        if request.dry_run:
            return ModelAdversarialPipelineResult(
                plan_path=plan_path,
                findings_count=findings_count,
                findings_summary=findings_summary,
                gate_passed=True,
                dry_run=True,
                stage_reached=stage_reached,
            )

        if self._ticket_adapter is None:
            raise RuntimeError("ticket adapter required when dry_run is false")
        ticket_result = self._ticket_adapter.create_tickets(
            {
                "topic": request.topic,
                "plan_path": plan_path,
                "linear_project": request.linear_project,
                "findings_count": findings_count,
                "findings_summary": findings_summary,
            }
        )
        created_ticket_ids = tuple(
            str(ticket_id)
            for ticket_id in ticket_result.get("created_ticket_ids", ()) or ()
        )
        return ModelAdversarialPipelineResult(
            plan_path=plan_path,
            findings_count=findings_count,
            findings_summary=findings_summary,
            gate_passed=True,
            created_ticket_ids=created_ticket_ids,
            tickets_created=int(
                ticket_result.get("tickets_created", len(created_ticket_ids)) or 0
            ),
            epic_url=_optional_str(ticket_result.get("epic_url")),
            dry_run=False,
            stage_reached=stage_reached,
        )


def _required_str(result: Mapping[str, Any], key: str, stage: str) -> str:
    value = result.get(key)
    if not isinstance(value, str) or not value.strip():
        raise RuntimeError(f"{stage} adapter did not return {key}")
    return value


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    return str(value)
