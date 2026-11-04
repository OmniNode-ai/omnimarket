# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Propose a carry-over sweep and replay it through the ordinary ask-state fold."""

from __future__ import annotations

from collections.abc import Callable

from omnimarket.models.model_ask_state import (
    EnumAskState,
    ModelAskStateReport,
    ModelAskStateRequest,
)
from omnimarket.models.model_carry_over import (
    ModelCarryOverCounts,
    ModelCarryOverPolicy,
    ModelCarryOverRequest,
    ModelCarryOverResult,
)
from omnimarket.nodes.node_ask_state_compute import HandlerAskState
from omnimarket.nodes.node_ask_state_effect.handlers.handler_ask_state_report import (
    HandlerAskStateReport,
    load_contract,
)


class HandlerCarryOver:
    """An apply requires an injected accepted-ledger append port; the default is read-only."""

    def __init__(
        self,
        append: Callable[[str], tuple[bool, str]] | None = None,
        reader: HandlerAskStateReport | None = None,
        fold: Callable[[ModelAskStateRequest], ModelAskStateReport] | None = None,
        policy: ModelCarryOverPolicy | None = None,
    ) -> None:
        self._append = append
        self._reader = reader or HandlerAskStateReport()
        self._fold = fold or HandlerAskState().handle
        self._policy = policy or ModelCarryOverPolicy.model_validate(
            load_contract()["carry_over"]
        )

    def handle(self, request: ModelCarryOverRequest) -> ModelCarryOverResult:
        policy = self._policy
        snapshot = self._reader.snapshot(request.read)
        before = self._fold(snapshot)
        cohort = frozenset(request.ask_ids)
        known = {e.ask_id: e for e in before.asks}
        if cohort - known.keys():
            raise ValueError(
                f"carry-over ids absent from source snapshot: {sorted(cohort - known.keys())}"
            )
        if any(e.ask_id not in cohort for e in request.evidence) or any(
            label.ask_id not in cohort for label in request.labels
        ):
            raise ValueError(
                "carry-over evidence or label names an ask outside the cohort"
            )
        if policy.owner != snapshot.config.default_owner:
            raise ValueError(
                "carry-over owner must match the ordinary fold's orchestrator owner"
            )
        stamp = request.read.now.strftime("%Y-%m-%dT%H:%M:%SZ")
        prefix = f"{stamp} | {{kind}} | lane={policy.lane}"
        rows: list[str] = []
        for evidence in request.evidence:
            if known[evidence.ask_id].state is EnumAskState.DONE:
                continue
            rows.append(
                prefix.format(kind="STATUS")
                + f" | ask={evidence.ask_id} | closes-ask={evidence.ask_id}"
                + f" | state={snapshot.config.done_state} | evidence={evidence.reference}"
            )
        decisions: list[str] = []
        for label in request.labels:
            if known[label.ask_id].state is EnumAskState.DONE:
                continue
            if label.label != policy.not_ask_label:
                continue
            if label.confirmed_by != policy.owner:
                decisions.append(label.ask_id)
                continue
            rows.append(
                prefix.format(kind="CORRECTION")
                + f" | ask={label.ask_id} | closes-ask={label.ask_id}"
                + f" | label={label.label} | label_run={label.run_id}"
                + f" | second_engine={label.engine} | confirmed_by={label.confirmed_by}"
            )
        proposed = self._fold(
            snapshot.model_copy(update={"rows": (*snapshot.rows, *rows)})
        )
        # Discard every completion candidate the SAME fold refused. No rejected 'done' row is published.
        completed = {e.ask_id for e in proposed.asks if e.state is EnumAskState.DONE}
        rows = [
            r
            for r in rows
            if any(f" | ask={identity} |" in r for identity in completed)
        ]
        for entry in proposed.asks:
            if entry.ask_id not in cohort or entry.state in (
                EnumAskState.DONE,
                EnumAskState.PARKED,
            ):
                continue
            if entry.state is EnumAskState.OWNED and entry.owner_lane == policy.owner:
                continue
            rows.append(
                prefix.format(kind="STATUS")
                + f" | ask={entry.ask_id} | {snapshot.config.state_cell}={snapshot.config.owned_state}"
                + f" | {snapshot.config.owner_cell}={policy.owner}"
            )
        report = self._fold(
            snapshot.model_copy(update={"rows": (*snapshot.rows, *rows)})
        )
        swept = [e for e in report.asks if e.ask_id in cohort]
        withdrawn = sum(e.resolution == "withdrawn" for e in swept)
        counts = ModelCarryOverCounts(
            total=len(swept),
            done=sum(e.state is EnumAskState.DONE for e in swept) - withdrawn,
            withdrawn=withdrawn,
            parked=sum(e.state is EnumAskState.PARKED for e in swept),
            owned=sum(e.state is EnumAskState.OWNED for e in swept),
        )
        if (
            counts.total
            != counts.done + counts.withdrawn + counts.owned + counts.parked
        ):
            raise ValueError("carry-over replay left an ask without a disposition")
        decisions = list(
            dict.fromkeys(
                identity for identity in decisions if identity not in completed
            )
        )
        text = (
            f"Carry-over: {counts.total} asks; {counts.done} done, {counts.withdrawn} withdrawn, "
            f"{counts.owned} owned, {counts.parked} parked; {len(decisions)} need a decision."
        )
        for identity in decisions[: policy.decision_limit]:
            text += f"\n- {identity}: confirm not-an-ask label: {known[identity].words}"
        if len(decisions) > policy.decision_limit:
            text += f"\n{len(decisions) - policy.decision_limit} further decisions in the structured result."
        applied = False
        if not request.dry_run and rows:
            if self._append is None:
                raise ValueError(
                    "carry-over apply requires an accepted-ledger append port"
                )
            ok, detail = self._append("\n".join(rows))
            if not ok:
                raise OSError(f"carry-over append refused: {detail}")
            applied = True
        return ModelCarryOverResult(
            dry_run=request.dry_run,
            proposed_rows=tuple(rows),
            report=report,
            counts=counts,
            decisions=tuple(decisions),
            text=text,
            applied=applied,
        )
