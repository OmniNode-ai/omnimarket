# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerLaneDispatchIntake: read MSG rows to=labdispatch and place each carried lane, one at a time.

The orchestrator's lane placement crashed the operator workstation when many placements ran in parallel, each building a
lane brief and starting a remote lane. The dispatcher runs on a lab host and places strictly one lane at a time: the brief
of a request is built and checked, the lane is placed detached, one STATUS row is written, and only then does the next
request start. A request already placed (its key ``<msg id>#<lane>`` is in ``done``) is never placed twice.

The MSG row format is ``parse_dispatch_rows``: a row ``<ts> | MSG | from=<lane> | id=<ts>-<lane> | to=labdispatch | ...
| requests=<base64url of a JSON array> | text``. Each array item has the keys ``lane``, ``ticket``, ``host`` and ``task``.
"""

from __future__ import annotations

import base64
import binascii
import json
from collections.abc import Iterable
from datetime import UTC, datetime

from pydantic import ValidationError

from omnimarket.nodes.node_lane_dispatch_intake_effect.models.model_lane_dispatch_intake import (
    EnumPlacementStatus,
    ModelDispatchMessage,
    ModelLaneDispatchIntakeRequest,
    ModelLaneDispatchIntakeResult,
    ModelPlacementOutcome,
    ModelPlacementRequest,
    ModelRejectedMessage,
)
from omnimarket.nodes.node_lane_dispatch_intake_effect.protocols import (
    BriefError,
    ProtocolBriefBuilder,
    ProtocolLanePlacer,
    ProtocolRowWriter,
    RowWriteError,
)

DISPATCHER_LANE = "labdispatch"
REQUESTS_CELL = "requests"
MAX_REQUESTS_PER_MESSAGE = 20


def encode_requests(requests: Iterable[ModelPlacementRequest]) -> str:
    """The ``requests=`` cell value for a list of placement requests."""
    payload = json.dumps(
        [r.model_dump(mode="json") for r in requests],
        separators=(",", ":"),
        sort_keys=True,
    )
    return base64.urlsafe_b64encode(payload.encode("utf-8")).decode("ascii").rstrip("=")


def _decode_requests(cell: str) -> list[object]:
    try:
        raw = base64.urlsafe_b64decode(cell + "=" * (-len(cell) % 4))
        decoded = json.loads(raw.decode("utf-8"))
    except (binascii.Error, ValueError, UnicodeDecodeError) as exc:
        raise ValueError(f"requests cell is not base64url JSON: {exc}") from exc
    if not isinstance(decoded, list) or not decoded:
        raise ValueError("requests cell is not a non-empty JSON array")
    if len(decoded) > MAX_REQUESTS_PER_MESSAGE:
        raise ValueError(
            f"requests cell holds {len(decoded)} requests, over {MAX_REQUESTS_PER_MESSAGE}"
        )
    return decoded


def _cells(row: str) -> dict[str, str]:
    cells: dict[str, str] = {}
    for part in row.split(" | ")[2:]:
        key, sep, value = part.partition("=")
        if sep and key.isidentifier():
            cells.setdefault(key, value.strip())
    return cells


def parse_dispatch_rows(
    rows: Iterable[str],
) -> tuple[list[ModelDispatchMessage], list[ModelRejectedMessage]]:
    """The valid dispatch messages in ledger order and the rejected ones; rows that are not MSG rows to the dispatcher are skipped."""
    messages: list[ModelDispatchMessage] = []
    rejected: list[ModelRejectedMessage] = []
    for row in rows:
        parts = row.split(" | ")
        if len(parts) < 3 or parts[1].strip() != "MSG":
            continue
        cells = _cells(row)
        if DISPATCHER_LANE not in [t.strip() for t in cells.get("to", "").split(",")]:
            continue
        msg_id = cells.get("id", "")
        if not msg_id:
            continue
        try:
            if REQUESTS_CELL not in cells:
                raise ValueError("no requests= cell")
            requests = tuple(
                ModelPlacementRequest.model_validate(item)
                for item in _decode_requests(cells[REQUESTS_CELL])
            )
            lanes = [r.lane for r in requests]
            if len(set(lanes)) != len(lanes):
                raise ValueError("a lane name appears twice in the message")
        except (ValueError, ValidationError) as exc:
            rejected.append(
                ModelRejectedMessage(
                    msg_id=msg_id, reason=str(exc).replace("\n", " ")[:300]
                )
            )
            continue
        messages.append(
            ModelDispatchMessage(
                msg_id=msg_id, sender=cells.get("from", ""), requests=requests
            )
        )
    return messages, rejected


def rejected_key(rejected: ModelRejectedMessage) -> str:
    return f"{rejected.msg_id}#rejected"


def status_row(
    outcome: ModelPlacementOutcome, msg_id: str, now: datetime | None = None
) -> str:
    stamp = (now or datetime.now(UTC)).strftime("%Y-%m-%dT%H:%M:%SZ")
    detail = " ".join(outcome.detail.split())[:200] or outcome.status.value
    return (
        f"{stamp} | STATUS | lane={DISPATCHER_LANE} | ticket={outcome.ticket} | placed={outcome.lane} "
        f"| status={outcome.status.value} | host={outcome.host} | exit={outcome.exit_code} "
        f"| msg={msg_id} | {detail}"
    )


def rejected_row(rejected: ModelRejectedMessage, now: datetime | None = None) -> str:
    stamp = (now or datetime.now(UTC)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return (
        f"{stamp} | STATUS | lane={DISPATCHER_LANE} | status=rejected | msg={rejected.msg_id} "
        f"| {rejected.reason}"
    )


class HandlerLaneDispatchIntake:
    """One tick: place at most ``max_placements`` pending requests, strictly one after another."""

    def __init__(
        self,
        briefs: ProtocolBriefBuilder,
        placer: ProtocolLanePlacer,
        writer: ProtocolRowWriter,
    ) -> None:
        self._briefs = briefs
        self._placer = placer
        self._writer = writer

    def handle(
        self, request: ModelLaneDispatchIntakeRequest
    ) -> ModelLaneDispatchIntakeResult:
        messages, rejected = parse_dispatch_rows(request.rows)
        pending = [
            (message, item)
            for message in messages
            for item in message.requests
            if message.key(item) not in request.done
        ]
        outcomes: list[ModelPlacementOutcome] = []
        for message, item in pending[: request.max_placements]:
            key = message.key(item)
            outcome = self._place_one(item, key, message.sender)
            if not (
                outcome.status is EnumPlacementStatus.NO_HOST and key in request.noted
            ):
                self._writer.append(status_row(outcome, message.msg_id))
            outcomes.append(outcome)
            if outcome.status is EnumPlacementStatus.NO_HOST:
                break  # no pool host admits a lane: the next request would meet the same refusal
        fresh_rejected = tuple(
            r for r in rejected if rejected_key(r) not in request.done
        )
        for refusal in fresh_rejected:
            self._writer.append(rejected_row(refusal))
        return ModelLaneDispatchIntakeResult(
            outcomes=tuple(outcomes),
            rejected=fresh_rejected,
            pending_after=len(pending) - len(outcomes),
        )

    def _place_one(
        self, item: ModelPlacementRequest, key: str, parent: str
    ) -> ModelPlacementOutcome:
        try:
            brief = self._briefs.build(item)
        except BriefError as exc:
            return ModelPlacementOutcome(
                key=key,
                lane=item.lane,
                ticket=item.ticket,
                status=EnumPlacementStatus.FAILED,
                exit_code=2,
                detail=f"brief: {exc}",
            )
        return self._placer.place(item, brief, key, parent)


__all__ = [
    "BriefError",
    "HandlerLaneDispatchIntake",
    "RowWriteError",
    "encode_requests",
    "parse_dispatch_rows",
    "rejected_key",
    "rejected_row",
    "status_row",
]
