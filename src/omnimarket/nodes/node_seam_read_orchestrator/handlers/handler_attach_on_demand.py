# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Attach one on-demand node to the bus, demand its command, read the result by the deadline (OMN-22909).

Piece 3 of the base-first plan: 440 of 577 loaded command nodes have an on-demand path only, so no
timestamp or consumer shows when a trigger is missing. The activation closes that gap for a test:
it attaches the node that consumes the seam's command topic, sends the command through the seam bus
(the deadline probe's port), reads the correlated result and always detaches. A seam is WORKING
only when the success result arrived by its deadline; an unattachable node, no result by the
deadline, an error answer or an unreadable bus is NOT_WORKING with the reason named.
"""

from __future__ import annotations

import json
from pathlib import Path

import yaml
from pydantic import ValidationError

from omnimarket.models.model_reading import EnumReadingVerdict
from omnimarket.nodes.node_seam_deadline_probe_effect.models.model_seam_deadline import (
    EnumSeamCrossing,
    ModelSeamDemand,
)
from omnimarket.nodes.node_seam_deadline_probe_effect.protocols import ProtocolSeamBus
from omnimarket.nodes.node_seam_read_orchestrator.models.model_attach_on_demand import (
    ModelAttachOnDemandRequest,
    ModelAttachOnDemandResult,
)
from omnimarket.nodes.node_seam_read_orchestrator.protocols import ProtocolNodeAttacher

CONTRACT = Path(__file__).resolve().parents[1] / "contract.yaml"


def load_topics(path: Path = CONTRACT) -> dict[str, str]:
    """Topics by role from the node's contract."""
    contract = yaml.safe_load(path.read_text(encoding="utf-8"))
    return dict(contract["topics"])


class HandlerAttachOnDemand:
    """Attach, demand, read, detach, over the injected attacher and bus."""

    def __init__(self, *, attacher: ProtocolNodeAttacher, bus: ProtocolSeamBus) -> None:
        self._attacher = attacher
        self._bus = bus

    def handle(self, request: ModelAttachOnDemandRequest) -> ModelAttachOnDemandResult:
        outcome = self._attacher.attach(request)
        if not outcome.attached:
            return ModelAttachOnDemandResult(
                correlation_id=request.correlation_id,
                seam=request.seam,
                status="failed",
                verdict=EnumReadingVerdict.NOT_WORKING,
                attached=False,
                detached=False,
                crossing=EnumSeamCrossing.ATTACH_FAILED,
                failure=f"attach-failed:{request.seam}:{outcome.detail or 'refused'}",
            )
        try:
            answer = self._bus.demand(
                ModelSeamDemand(
                    seam=request.seam,
                    command_topic=request.command_topic,
                    result_topic=request.result_topic,
                    consumer_group_prefix=request.consumer_group_prefix,
                    correlation_id=request.correlation_id,
                    payload=request.payload,
                    error_keys=request.error_keys,
                    deadline_seconds=request.deadline_seconds,
                )
            )
        finally:
            self._attacher.detach(request)
        crossed = answer.crossing is EnumSeamCrossing.RESULT
        return ModelAttachOnDemandResult(
            correlation_id=request.correlation_id,
            seam=request.seam,
            status="completed" if crossed else "failed",
            verdict=EnumReadingVerdict.WORKING
            if crossed
            else EnumReadingVerdict.NOT_WORKING,
            attached=True,
            detached=True,
            crossing=answer.crossing,
            elapsed_ms=answer.elapsed_ms,
            failure=None
            if crossed
            else f"seam-not-crossed:{request.seam}:{answer.crossing.value}",
        )


def events_of(
    result: ModelAttachOnDemandResult, topics: dict[str, str]
) -> list[tuple[str, dict[str, object]]]:
    """The events a run produces, in order, all under its correlation id."""
    events: list[tuple[str, dict[str, object]]] = [
        (
            topics["attach_on_demand_started"],
            {"correlation_id": result.correlation_id, "seam": result.seam},
        )
    ]
    if result.attached:
        events.append(
            (
                topics["on_demand_node_attached"],
                {"correlation_id": result.correlation_id, "seam": result.seam},
            )
        )
    final: dict[str, object] = {
        "correlation_id": result.correlation_id,
        "seam": result.seam,
        "verdict": result.verdict.value,
        "attached": result.attached,
        "detached": result.detached,
        "crossing": result.crossing.value if result.crossing else None,
        "elapsed_ms": result.elapsed_ms,
    }
    if result.status == "failed":
        final["failure"] = result.failure
        events.append((topics["attach_on_demand_failed"], final))
    else:
        events.append((topics["attach_on_demand_completed"], final))
    return events


def answer_command(
    payload: bytes, handler: HandlerAttachOnDemand, topics: dict[str, str]
) -> list[tuple[str, bytes]]:
    """One command message in, the events it produces out, each as (topic, JSON bytes).

    A payload that is not a request is answered with a typed failure event, never dropped.
    """
    try:
        request = ModelAttachOnDemandRequest.model_validate_json(payload)
    except ValidationError as exc:
        fields = sorted(
            {".".join(str(p) for p in e["loc"]) or "<document>" for e in exc.errors()}
        )
        failure = {"correlation_id": "unknown", "failure": f"bad-payload:{fields}"}
        return [(topics["attach_on_demand_failed"], json.dumps(failure).encode())]
    return [
        (topic, json.dumps(body, sort_keys=True).encode())
        for topic, body in events_of(handler.handle(request), topics)
    ]


__all__ = ["HandlerAttachOnDemand", "answer_command", "events_of", "load_topics"]
