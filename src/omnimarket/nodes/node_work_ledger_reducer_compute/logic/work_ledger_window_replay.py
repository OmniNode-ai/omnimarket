# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The window replay: a trailing window of accepted rows and the refused requests, re-judged (OMN-25629, L11).

Each accepted row in the last N days is judged against the rows the live judge accepted before it, and must
be accepted. Each refused request is judged against the rows accepted before its time, and must be refused
under the rule name the file judge gave. The window can hold hundreds of thousands of rows, so a row is not
judged against the whole ledger text: the guards read only the rows that share its id, its surface lease and
its lane's CLAIMs (``ledger_row_grammar.refusal_for_state``), and those are the rows handed to the judge. A
payload the index cannot bound (a CORRECTION naming an ``operator_span``) is judged against the whole text.
The judge is injected, so a deliberately altered guard can be shown to make the difference list non-empty.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from omnimarket.handlers.ledger_row_grammar import ID_ROW_TYPES, Row, parse_row
from omnimarket.nodes.node_work_ledger_reducer_compute.logic.work_ledger_judge import (
    judge_row,
    rule_of,
    verdict_difference,
)
from omnimarket.nodes.node_work_ledger_reducer_compute.logic.work_ledger_shadow_judge import (
    JudgeFn,
)
from omnimarket.nodes.node_work_ledger_reducer_compute.models.model_work_ledger_judge import (
    EnumJudgeVerdict,
    ModelJudgeVerdict,
    ModelReplayDifference,
)
from omnimarket.nodes.node_work_ledger_reducer_compute.models.model_work_ledger_window_replay import (
    ModelRefusedRequest,
    ModelWindowReplayRequest,
    ModelWindowReplayResult,
)

_ACCEPT = ModelJudgeVerdict(verdict=EnumJudgeVerdict.ACCEPT)
_STAMP_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


def _stamp_time(stamp: str) -> datetime:
    return datetime.fromisoformat(stamp.replace("Z", "+00:00")).astimezone(UTC)


def recorded_refusal(message: str) -> ModelJudgeVerdict:
    """The refusal a receipt message records, its rule name the text before the first separator."""
    return ModelJudgeVerdict(
        verdict=EnumJudgeVerdict.REFUSE, rule=rule_of(message), reason=message
    )


class _StateIndex:
    """The accepted rows in order, indexed by what the state guards read."""

    def __init__(self) -> None:
        self.rows: list[str] = []
        self._by_id: dict[str, list[int]] = {}
        self._holds_by_surface: dict[str, list[int]] = {}
        self._hold_id_at: dict[int, str] = {}
        self._releases_of: dict[str, list[int]] = {}
        self._claims_by_lane: dict[str, list[int]] = {}

    def add(self, line: str) -> None:
        position = len(self.rows)
        self.rows.append(line)
        row = parse_row(line)
        if row is None:
            return
        if row.row_type in ID_ROW_TYPES and (value := row.token("id")):
            self._by_id.setdefault(value, []).append(position)
        if row.row_type == "HOLD":
            surface = row.value("surface")
            hold_id = row.token("id")
            if surface and hold_id:
                self._holds_by_surface.setdefault(surface.strip(), []).append(position)
                self._hold_id_at[position] = hold_id
        elif row.row_type == "RELEASE" and (target := row.token("re")):
            self._releases_of.setdefault(target, []).append(position)
        elif row.row_type == "CLAIM" and (lane := row.token("lane")):
            self._claims_by_lane.setdefault(lane, []).append(position)

    def _positions_for(self, row: Row) -> set[int]:
        found: set[int] = set()
        if row.row_type in ID_ROW_TYPES and (value := row.token("id")):
            found.update(self._by_id.get(value, ()))
        if row.row_type == "HOLD" and (surface := row.value("surface")):
            for position in self._holds_by_surface.get(surface.strip(), ()):
                found.add(position)
                found.update(self._releases_of.get(self._hold_id_at[position], ()))
        if row.row_type == "TERMINAL" and (lane := row.token("lane")):
            found.update(self._claims_by_lane.get(lane, ()))
        return found

    def relevant_text(self, payload: str) -> str:
        """The accepted rows the state guards can read for ``payload``, in ledger order."""
        found: set[int] = set()
        for line in payload.splitlines():
            row = parse_row(line)
            if row is None:
                continue
            if row.row_type == "CORRECTION" and "operator_span" in row.fields:
                return "\n".join(self.rows)
            found.update(self._positions_for(row))
        return "\n".join(self.rows[position] for position in sorted(found))


class WorkLedgerWindowReplay:
    def __init__(self, judge: JudgeFn = judge_row) -> None:
        self._judge = judge

    def handle(self, request: ModelWindowReplayRequest) -> ModelWindowReplayResult:
        stamps = [
            row.stamp for line in request.accepted_rows if (row := parse_row(line))
        ]
        if not stamps:
            return ModelWindowReplayResult(
                rows_judged=0,
                refused_judged=0,
                unreadable_refusals=request.unreadable_refusals,
                equivalent=not request.unreadable_refusals,
            )
        window_end = max(_stamp_time(stamp) for stamp in stamps)
        window_start = window_end - timedelta(days=request.window_days)
        start_stamp = window_start.strftime(_STAMP_FORMAT)

        refused = sorted(
            (
                (item.requested_at.astimezone(UTC).strftime(_STAMP_FORMAT), item)
                for item in request.refused_requests
                if item.requested_at >= window_start
            ),
            key=lambda pair: pair[0],
        )
        refused_stamps = [pair[0] for pair in refused]
        index = _StateIndex()
        row_differences: list[ModelReplayDifference] = []
        refusal_differences: list[ModelReplayDifference] = []
        rows_judged = 0
        next_refusal = 0

        def judge_refusals_before(stamp: str | None) -> None:
            nonlocal next_refusal
            while next_refusal < len(refused) and (
                stamp is None or refused_stamps[next_refusal] < stamp
            ):
                _, item = refused[next_refusal]
                next_refusal += 1
                difference = self._refusal_difference(
                    request, item, index, next_refusal
                )
                if difference is not None:
                    refusal_differences.append(difference)

        for line in request.accepted_rows:
            row = parse_row(line)
            if row is None:
                index.add(line)
                continue
            judge_refusals_before(row.stamp)
            if row.stamp >= start_stamp:
                rows_judged += 1
                replayed = self._judge(
                    line, index.relevant_text(line), request.ledger_name
                )
                text = verdict_difference(
                    _ACCEPT, replayed, names=("recorded", "replayed")
                )
                if text is not None:
                    row_differences.append(
                        ModelReplayDifference(
                            position=rows_judged,
                            row_ref=f"{row.stamp} {row.row_type}",
                            recorded=_ACCEPT,
                            replayed=replayed,
                            difference=text,
                        )
                    )
            index.add(line)
        judge_refusals_before(None)
        return ModelWindowReplayResult(
            window_start=window_start,
            window_end=window_end,
            rows_judged=rows_judged,
            refused_judged=len(refused),
            row_differences=tuple(row_differences),
            refusal_differences=tuple(refusal_differences),
            unreadable_refusals=request.unreadable_refusals,
            equivalent=not (
                row_differences or refusal_differences or request.unreadable_refusals
            ),
        )

    def _refusal_difference(
        self,
        request: ModelWindowReplayRequest,
        item: ModelRefusedRequest,
        index: _StateIndex,
        position: int,
    ) -> ModelReplayDifference | None:
        recorded = recorded_refusal(item.file_judge_message)
        replayed = self._judge(
            item.rows, index.relevant_text(item.rows), request.ledger_name
        )
        text = verdict_difference(recorded, replayed, names=("file judge", "replayed"))
        if text is None:
            return None
        return ModelReplayDifference(
            position=position,
            row_ref=item.request_id,
            recorded=recorded,
            replayed=replayed,
            difference=text,
        )


__all__: list[str] = ["WorkLedgerWindowReplay", "recorded_refusal"]
