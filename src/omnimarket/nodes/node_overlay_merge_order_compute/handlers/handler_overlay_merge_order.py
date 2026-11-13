# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Decide whether an overlay change is ahead of the code that models it (OMN-22809).

A pure decision over typed facts: the overlays a change touches and the merged tree's answer for each. It reads
no file, runs no process and writes nothing. The order it keeps is the one piece 4's daily drift read measures:
an overlay whose key the running model refuses reads the piece NOT WORKING (the rung_order case), so the model
merges first and the overlay second.
"""

from __future__ import annotations

import json
import re

from pydantic import ValidationError

from omnimarket.enums.enum_overlay_verdict import EnumOverlayVerdict
from omnimarket.nodes.node_overlay_merge_order_compute.models.model_overlay_merge_order import (
    OVERLAY_ID_PATTERN,
    EnumMergeOrderVerdict,
    ModelAheadOverlay,
    ModelOverlayMergeOrderCommand,
    ModelOverlayMergeOrderResult,
)
from omnimarket.nodes.node_overlay_merge_order_compute.overlay import (
    ModelOverlayMergeOrderOverlay,
)

_OVERLAY_ID = re.compile(OVERLAY_ID_PATTERN)


class HandlerOverlayMergeOrder:
    """``handle(request) -> result``: ORDER_OK, AHEAD_OF_CODE, or a typed failure that refuses the check."""

    def __init__(self, overlay: ModelOverlayMergeOrderOverlay) -> None:
        self._overlay = overlay

    def handle(
        self, request: ModelOverlayMergeOrderCommand
    ) -> ModelOverlayMergeOrderResult:
        if not request.changed_overlays:
            return self._refused(request, "no_changed_overlays")
        changed = tuple(sorted(set(request.changed_overlays)))
        malformed = [name for name in changed if not _OVERLAY_ID.fullmatch(name)]
        if malformed:
            return self._refused(request, f"bad_overlay_id:{'+'.join(malformed)}")

        by_id: dict[str, list[EnumOverlayVerdict]] = {}
        fields: dict[str, tuple[str, ...]] = {}
        for check in request.merged_checks:
            by_id.setdefault(check.overlay_id, []).append(check.verdict)
            fields[check.overlay_id] = check.fields
        doubled = sorted(name for name in changed if len(by_id.get(name, ())) > 1)
        if doubled:
            return self._refused(request, f"duplicate_check:{'+'.join(doubled)}")
        unjudged = [name for name in changed if name not in by_id]
        if unjudged:
            return self._refused(
                request, f"changed_overlay_not_judged:{'+'.join(unjudged)}"
            )

        accepted: list[str] = []
        ahead: list[ModelAheadOverlay] = []
        for name in changed:
            verdict = by_id[name][0]
            if verdict is EnumOverlayVerdict.ACCEPTED:
                accepted.append(name)
            elif verdict in self._overlay.ahead_verdicts:
                ahead.append(
                    ModelAheadOverlay(
                        overlay_id=name, verdict=verdict.value, fields=fields[name]
                    )
                )
            else:
                return self._refused(
                    request, f"verdict_not_ruled:{name}:{verdict.value}"
                )
        return ModelOverlayMergeOrderResult(
            correlation_id=request.correlation_id,
            status="completed",
            verdict=EnumMergeOrderVerdict.AHEAD_OF_CODE
            if ahead
            else EnumMergeOrderVerdict.ORDER_OK,
            merged_ref=request.merged_ref,
            changed=len(changed),
            accepted=tuple(accepted),
            ahead=tuple(ahead),
            injected_fault=self._overlay.injected_fault,
        )

    @staticmethod
    def _refused(
        request: ModelOverlayMergeOrderCommand, failure: str
    ) -> ModelOverlayMergeOrderResult:
        return ModelOverlayMergeOrderResult(
            correlation_id=request.correlation_id,
            status="failed",
            failure=failure,
            merged_ref=request.merged_ref,
        )


def events_of(
    result: ModelOverlayMergeOrderResult, topics: dict[str, str], max_named: int
) -> list[tuple[str, dict[str, object]]]:
    """The events a run produces, in order, all under its correlation id."""
    if result.status == "failed":
        return [
            (
                topics["overlay_merge_order_run_failed"],
                {"correlation_id": result.correlation_id, "failure": result.failure},
            )
        ]
    assert result.verdict is not None
    return [
        (
            topics["overlay_merge_order_decided"],
            {
                "correlation_id": result.correlation_id,
                "verdict": result.verdict.value,
                "merged_ref": result.merged_ref,
                "changed": result.changed,
                "accepted": list(result.accepted),
                "ahead_count": len(result.ahead),
                "ahead": [
                    item.model_dump(mode="json") for item in result.ahead[:max_named]
                ],
                "injected_fault": result.injected_fault,
            },
        ),
        (
            topics["overlay_merge_order_run_completed"],
            {
                "correlation_id": result.correlation_id,
                "verdict": result.verdict.value,
                "failed": 0,
            },
        ),
    ]


def answer_command(
    payload: bytes,
    handler: HandlerOverlayMergeOrder,
    topics: dict[str, str],
    max_named: int,
) -> list[tuple[str, bytes]]:
    """One command message in, the events it produces out, each as (topic, JSON bytes).

    A payload that is not a command is answered with a typed failure event, never dropped.
    """
    try:
        command = ModelOverlayMergeOrderCommand.model_validate_json(payload)
    except ValidationError as exc:
        fields = sorted(
            {".".join(str(p) for p in e["loc"]) or "<document>" for e in exc.errors()}
        )
        failure = {"correlation_id": "unknown", "failure": f"bad-payload:{fields}"}
        return [
            (topics["overlay_merge_order_run_failed"], json.dumps(failure).encode())
        ]
    return [
        (topic, json.dumps(body, sort_keys=True).encode())
        for topic, body in events_of(handler.handle(command), topics, max_named)
    ]


__all__ = ["HandlerOverlayMergeOrder", "answer_command", "events_of"]
