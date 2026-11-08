# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Read each declared seam against its sender: a required automatic seam with none is NOT WORKING (OMN-22909).

Pure: declarations, the verdicts the attach-on-demand activation returned, and ``now``. A required
automatic seam with no bound sender can never cross, so it reads NOT_WORKING with reason
``no-bound-sender``; the same seam with a sender reads WORKING. An optional automatic seam with
no sender is not a failure. An on-demand seam reads the verdict its demand returned, UNKNOWN when
it was never demanded.
"""

from __future__ import annotations

import json
from pathlib import Path

from pydantic import ValidationError

from omnimarket.models.model_reading import EnumReadingVerdict
from omnimarket.models.seam_reading.model_required_sender import (
    EnumSeamTrigger,
    ModelRequiredSenderRequest,
    ModelRequiredSenderResult,
    ModelSeamCounts,
    ModelSeamDeclaration,
    ModelSeamVerdict,
)
from omnimarket.nodes.contract_topics import contract_section

CONTRACT = Path(__file__).resolve().parents[1] / "contract.yaml"


def load_topics(path: Path = CONTRACT) -> dict[str, str]:
    """Topics by role from the node's contract."""
    topics = contract_section(path, "topics")
    if not isinstance(topics, dict):
        raise ValueError(f"{path}: topics must be a mapping")
    return {str(role): str(topic) for role, topic in topics.items()}


def _automatic(declaration: ModelSeamDeclaration) -> tuple[EnumReadingVerdict, str]:
    if declaration.sender is not None:
        return EnumReadingVerdict.WORKING, "sender-bound"
    if declaration.required:
        return EnumReadingVerdict.NOT_WORKING, "no-bound-sender"
    return EnumReadingVerdict.WORKING, "optional-unbound"


class HandlerRequiredSender:
    """Verdict and counts of a set of declared seams."""

    def handle(self, request: ModelRequiredSenderRequest) -> ModelRequiredSenderResult:
        demanded = {d.seam: d.verdict for d in request.demanded}
        verdicts: list[ModelSeamVerdict] = []
        for declaration in request.declarations:
            if declaration.trigger is EnumSeamTrigger.AUTOMATIC:
                verdict, reason = _automatic(declaration)
            elif declaration.seam in demanded:
                verdict, reason = demanded[declaration.seam], "demanded"
            else:
                verdict, reason = EnumReadingVerdict.UNKNOWN, "not-demanded"
            verdicts.append(
                ModelSeamVerdict(
                    seam=declaration.seam,
                    trigger=declaration.trigger,
                    verdict=verdict,
                    reason=reason,
                )
            )

        def count(wanted: EnumReadingVerdict) -> int:
            return sum(v.verdict is wanted for v in verdicts)

        counts = ModelSeamCounts(
            automatic=sum(
                d.trigger is EnumSeamTrigger.AUTOMATIC for d in request.declarations
            ),
            on_demand=sum(
                d.trigger is EnumSeamTrigger.ON_DEMAND for d in request.declarations
            ),
            required_without_sender=sum(
                v.reason == "no-bound-sender" for v in verdicts
            ),
            working=count(EnumReadingVerdict.WORKING),
            not_working=count(EnumReadingVerdict.NOT_WORKING),
            unknown=count(EnumReadingVerdict.UNKNOWN),
        )
        overall = (
            EnumReadingVerdict.NOT_WORKING
            if counts.not_working
            else EnumReadingVerdict.UNKNOWN
            if counts.unknown
            else EnumReadingVerdict.WORKING
        )
        return ModelRequiredSenderResult(
            verdicts=tuple(verdicts),
            counts=counts,
            verdict=overall,
            rendered_at=request.now,
        )


def answer_command(
    payload: bytes, handler: HandlerRequiredSender, topics: dict[str, str]
) -> list[tuple[str, bytes]]:
    """One request in, one event out as (topic, JSON bytes): completed when every seam reads
    WORKING, failed when any does not or the payload is not a request. Nothing is dropped."""
    try:
        request = ModelRequiredSenderRequest.model_validate_json(payload)
    except ValidationError as exc:
        fields = sorted(
            {".".join(str(p) for p in e["loc"]) or "<document>" for e in exc.errors()}
        )
        failure = {"verdict": "UNKNOWN", "failure": f"bad-payload:{fields}"}
        return [(topics["required_sender_read_failed"], json.dumps(failure).encode())]
    result = handler.handle(request)
    body: dict[str, object] = {
        "verdict": result.verdict.value,
        "counts": result.counts.model_dump(),
        "seams": {v.seam: f"{v.verdict.value}:{v.reason}" for v in result.verdicts},
    }
    topic = topics[
        "required_sender_read_completed"
        if result.verdict is EnumReadingVerdict.WORKING
        else "required_sender_read_failed"
    ]
    if result.verdict is not EnumReadingVerdict.WORKING:
        body["failure"] = ",".join(
            f"{v.seam}:{v.reason}"
            for v in result.verdicts
            if v.verdict is not EnumReadingVerdict.WORKING
        )
    return [(topic, json.dumps(body, sort_keys=True).encode())]


__all__ = ["HandlerRequiredSender", "answer_command", "load_topics"]
