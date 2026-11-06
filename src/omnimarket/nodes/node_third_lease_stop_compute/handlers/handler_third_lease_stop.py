# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A cause leased a third time stops further leases until a go-and-see record exists (OMN-21955).

Pure over ledger rows. A lease is a ledger row of the contract's lease type that names a cause; the
landing controller writes it as a pair (its cause lane and the worker), which the grammar's lane
prefixes fold into one. A go-and-see record is a row of the contract's record type that names the cause.

For a cause with ``m`` leases the next lease is number ``m + 1``. The stop applies from the configured
lease count: the next lease is refused when ``m + 1`` reaches it and no record is dated after lease ``m``.
The same rule read over history yields the leases the stop would have refused, which is the report-only
count. The model is the andon stop (formal/andon_stop/AndonStop.tla): the stop clears only on evidence,
here the record, never on the passage of time.

The configuration is data (``ModelThirdLeaseStopConfig``): no count, mode, cell name or window in here.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta

from omnimarket.nodes.node_third_lease_stop_compute.models.model_third_lease_stop import (
    EnumThirdLeaseStopDecision,
    EnumThirdLeaseStopMode,
    ModelCauseDecision,
    ModelLeaseRowGrammar,
    ModelThirdLeaseStopRequest,
    ModelThirdLeaseStopResult,
    ModelWouldStop,
)

_CELL_SEPARATOR = " | "


def parse_row(row: str) -> tuple[datetime, str, dict[str, str]] | None:
    """``(stamp, type, cells)`` of a ledger row, or None when its stamp or type cannot be read."""
    head, separator, rest = row.strip().partition(_CELL_SEPARATOR)
    row_type, _, tail = rest.partition(_CELL_SEPARATOR)
    if not separator or not row_type.strip():
        return None
    try:
        stamp = datetime.fromisoformat(head.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if stamp.tzinfo is None:
        return None
    named: dict[str, str] = {}
    for cell in tail.split(_CELL_SEPARATOR):
        key, sep, value = cell.strip().partition("=")
        if sep and key and " " not in key and key not in named:
            named[key] = value.strip()
    return stamp, row_type.strip(), named


def lease_id(lane: str, grammar: ModelLeaseRowGrammar) -> str:
    """The lease a lane name stands for: the longest listed prefix stripped, else the lane itself."""
    for prefix in sorted(grammar.lease_lane_prefixes, key=len, reverse=True):
        if lane.startswith(prefix) and len(lane) > len(prefix):
            return lane[len(prefix) :]
    return lane


class HandlerThirdLeaseStop:
    """``handle(ModelThirdLeaseStopRequest) -> ModelThirdLeaseStopResult``."""

    def handle(self, request: ModelThirdLeaseStopRequest) -> ModelThirdLeaseStopResult:
        config = request.config
        grammar = config.grammar
        # cause -> lease id -> earliest stamp; cause -> record stamps
        leases: dict[str, dict[str, datetime]] = defaultdict(dict)
        records: dict[str, list[datetime]] = defaultdict(list)
        unreadable = 0
        records_seen = 0
        for row in request.rows:
            parsed = parse_row(row)
            if parsed is None:
                unreadable += 1
                continue
            stamp, row_type, cells = parsed
            if row_type == grammar.lease_row_type:
                cause = cells.get(grammar.cause_cell, "")
                if not cause:
                    continue
                lease = lease_id(cells.get(grammar.lane_cell, ""), grammar) or cause
                known = leases[cause].get(lease)
                if known is None or stamp < known:
                    leases[cause][lease] = stamp
            elif row_type == grammar.record_row_type:
                cause = cells.get(grammar.record_cell, "")
                if cause:
                    records[cause].append(stamp)
                    records_seen += 1

        causes = sorted(leases)
        if request.cause is not None:
            causes = [request.cause]
        window_start = request.now - timedelta(hours=config.replay_window_hours)
        enforcing = config.mode is EnumThirdLeaseStopMode.ENFORCE

        decisions: list[ModelCauseDecision] = []
        would_stop: list[ModelWouldStop] = []
        for cause in causes:
            ordered = sorted((at, lid) for lid, at in leases.get(cause, {}).items())
            record_times = sorted(records.get(cause, ()))
            decisions.append(
                self._gate(cause, ordered, record_times, config.lease_count, enforcing)
            )
            for index, (at, lid) in enumerate(ordered):
                number = index + 1
                if number < config.lease_count or not (
                    window_start <= at <= request.now
                ):
                    continue
                previous = ordered[index - 1][0]
                if any(previous < r <= at for r in record_times):
                    continue
                would_stop.append(
                    ModelWouldStop(
                        cause=cause,
                        lease_number=number,
                        lease_id=lid,
                        lease_at=at,
                        previous_lease_at=previous,
                        mode=config.mode,
                        stop_class_id=config.stop_class_id,
                    )
                )

        stopped = sum(
            1 for d in decisions if d.decision is EnumThirdLeaseStopDecision.STOP
        )
        line = (
            f"third_lease_stop mode={config.mode.value} lease_count={config.lease_count} "
            f"window_hours={config.replay_window_hours} causes={len(causes)} "
            f"would_stop={len(would_stop)} gate_stopped={stopped} "
            f"leases={sum(len(leases.get(c, {})) for c in causes)} records={records_seen} "
            f"rows_unreadable={unreadable}"
        )
        return ModelThirdLeaseStopResult(
            mode=config.mode,
            lease_count_threshold=config.lease_count,
            decisions=tuple(decisions),
            would_stop=tuple(would_stop),
            blocks=any(d.enforced for d in decisions),
            causes_seen=len(causes),
            leases_seen=sum(len(leases.get(c, {})) for c in causes),
            records_seen=records_seen,
            rows_unreadable=unreadable,
            line=line,
        )

    @staticmethod
    def _gate(
        cause: str,
        ordered: list[tuple[datetime, str]],
        record_times: list[datetime],
        threshold: int,
        enforcing: bool,
    ) -> ModelCauseDecision:
        held = len(ordered)
        last = ordered[-1][0] if ordered else None
        recorded = [r for r in record_times if last is not None and r > last]
        needs_record = held + 1 >= threshold and held > 0
        if needs_record and not recorded:
            return ModelCauseDecision(
                cause=cause,
                lease_count=held,
                decision=EnumThirdLeaseStopDecision.STOP,
                enforced=enforcing,
                last_lease_at=last,
                reason=(
                    f"lease {held + 1} would reach the stop count {threshold} and no "
                    f"go-and-see record is dated after lease {held}"
                ),
            )
        return ModelCauseDecision(
            cause=cause,
            lease_count=held,
            decision=EnumThirdLeaseStopDecision.ADMIT,
            enforced=False,
            last_lease_at=last,
            go_and_see_at=recorded[0] if recorded else None,
            reason=(
                "a go-and-see record is dated after the last lease"
                if needs_record
                else f"lease {held + 1} is below the stop count {threshold}"
            ),
        )
