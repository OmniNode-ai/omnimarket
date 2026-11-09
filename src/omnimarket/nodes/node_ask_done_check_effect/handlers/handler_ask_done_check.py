# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""An operator ask's done check reads the running system and records its verdict on the ledger.

Orchestrator 2026-11-06T14:27:51Z (piece 6): an operator ask closes only when a done check that
reads the running system reads true, never on a lane's report or a merge. This node runs each
ask's declared check and appends one STATUS row on the check lane citing the ask with its
verdict; node_ask_state_compute closes an ask on a TRUE row only.

* ``floor:piece=<n>,status=<s>``: the floor's JSON shows piece n with status s. A floor older
  than the overlay's bound, or unreadable, is UNKNOWN.
* ``row:lane=<lane>[,type=<TYPE>],<cell>=<value>...``: the ledger, read through the bus-fed
  query node, holds a row of that lane and type stamped after the ask whose cells match. A read
  that fails is UNKNOWN, never FALSE and never TRUE.

The golden event is a TRUE verdict row; the error event is an UNKNOWN row (the check could not
read). Named break: a done check whose source cannot be read reads UNKNOWN and leaves the ask open.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Literal

from omnimarket.nodes.node_ask_done_check_effect.models import (
    EnumCheckVerdict,
    ModelAskCheckVerdict,
    ModelAskDoneCheckOverlay,
    ModelAskDoneCheckRequest,
    ModelAskDoneCheckResult,
    ModelAskToCheck,
)
from omnimarket.nodes.node_ask_done_check_effect.protocols import ProtocolFloorRead
from omnimarket.nodes.node_reading_writer_effect.protocols import (
    ModelLedgerQuery,
    ProtocolLedgerRowSource,
    ProtocolReadingAppender,
)

_STAMP = "%Y-%m-%dT%H:%M:%SZ"
_DEFAULT_ROW_TYPE = "STATUS"


def parse_spec(spec: str) -> tuple[str, dict[str, str]]:
    kind, _, body = spec.partition(":")
    cells = dict(part.split("=", 1) for part in body.split(","))
    return kind, cells


class HandlerAskDoneCheck:
    """``handle(ModelAskDoneCheckRequest) -> ModelAskDoneCheckResult``."""

    def __init__(
        self,
        overlay: ModelAskDoneCheckOverlay,
        floor: ProtocolFloorRead,
        rows: ProtocolLedgerRowSource,
        appender: ProtocolReadingAppender,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        new_id: Callable[[], str] = lambda: str(uuid.uuid4()),
    ) -> None:
        self._overlay = overlay
        self._floor = floor
        self._rows = rows
        self._appender = appender
        self._clock = clock
        self._new_id = new_id

    def handle(self, request: ModelAskDoneCheckRequest) -> ModelAskDoneCheckResult:
        verdicts = []
        for ask in request.asks:
            now = self._clock().astimezone(UTC)
            verdict, reason = self._check(ask, now)
            row = self._row(ask, verdict, reason, now)
            written: Literal["yes", "no", "failed"] = "no"
            if request.write:
                outcome = self._appender.append(row, self._new_id())
                written = "yes" if outcome.ok else "failed"
            verdicts.append(
                ModelAskCheckVerdict(
                    ask_id=ask.ask_id,
                    done_check=ask.done_check,
                    verdict=verdict,
                    reason=reason,
                    row=row,
                    written=written,
                )
            )
        return ModelAskDoneCheckResult(verdicts=tuple(verdicts))

    def _check(
        self, ask: ModelAskToCheck, now: datetime
    ) -> tuple[EnumCheckVerdict, str]:
        kind, cells = parse_spec(ask.done_check)
        if kind == "floor":
            return self._floor_check(cells, now)
        return self._row_check(ask, cells, now)

    def _floor_check(
        self, cells: dict[str, str], now: datetime
    ) -> tuple[EnumCheckVerdict, str]:
        if "piece" not in cells or not cells["piece"].isdigit():
            return EnumCheckVerdict.UNKNOWN, "floor-check-names-no-piece"
        floor = self._floor.read()
        if floor.error is not None:
            return EnumCheckVerdict.UNKNOWN, floor.error
        try:
            generated = datetime.fromisoformat(
                (floor.generated_at or "").replace("Z", "+00:00")
            )
        except ValueError:
            return EnumCheckVerdict.UNKNOWN, "floor-without-generated-at"
        age = (now - generated.astimezone(UTC)).total_seconds()
        if age > self._overlay.floor.max_age_seconds:
            return EnumCheckVerdict.UNKNOWN, "floor-older-than-bound"
        status = floor.pieces.get(int(cells["piece"]))
        if status is None:
            return EnumCheckVerdict.UNKNOWN, "floor-has-no-such-piece"
        wanted = cells.get("status", "working")
        if status == wanted:
            return EnumCheckVerdict.TRUE, f"floor-piece-{cells['piece']}-{status}"
        return (
            EnumCheckVerdict.FALSE,
            f"floor-piece-{cells['piece']}-{status or 'none'}",
        )

    def _row_check(
        self, ask: ModelAskToCheck, cells: dict[str, str], now: datetime
    ) -> tuple[EnumCheckVerdict, str]:
        lane = cells.pop("lane", None)
        if lane is None:
            return EnumCheckVerdict.UNKNOWN, "row-check-names-no-lane"
        row_type = cells.pop("type", _DEFAULT_ROW_TYPE)
        # The first cell narrows the read node's search, so a busy lane's rows fit one page.
        term = next((f"{k}={v}" for k, v in cells.items()), None)
        answer = self._rows.read(
            ModelLedgerQuery(
                since=ask.said_at, until=now, row_type=row_type, lane=lane, term=term
            )
        )
        if answer.error is not None:
            return EnumCheckVerdict.UNKNOWN, f"row-read-failed-{answer.error}"
        for row in answer.rows:
            if row.stamp >= ask.said_at and all(
                row.cells.get(k) == v for k, v in cells.items()
            ):
                return EnumCheckVerdict.TRUE, f"row-found-{row.stamp.strftime(_STAMP)}"
        return EnumCheckVerdict.FALSE, "no-matching-row-since-the-ask"

    def _row(
        self,
        ask: ModelAskToCheck,
        verdict: EnumCheckVerdict,
        reason: str,
        now: datetime,
    ) -> str:
        cells = [
            f"lane={self._overlay.check_lane}",
            "src=ask-check",
            f"actor={self._overlay.actor}",
            "model=none",
            f"ask={ask.ask_id}",
            f"done_check={ask.done_check}",
            f"verdict={verdict.value}",
            f"reason={reason}",
            "the done check read the running system",
        ]
        return f"{now.strftime(_STAMP)} | STATUS | " + " | ".join(cells) + "\n"


__all__ = ["HandlerAskDoneCheck", "parse_spec"]
