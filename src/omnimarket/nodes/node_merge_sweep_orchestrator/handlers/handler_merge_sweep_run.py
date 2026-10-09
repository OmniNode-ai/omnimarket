# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Run one whole merge sweep: read the fleet, plan the lanes, brief and dispatch each (OMN-20676).

The port of what ``merge_sweep.js`` did in one script, as a chain of the sibling nodes' stages:
the effect node loads the facts from the watcher state, the reading node reads them, the plan node
caps the lanes, renders each brief and decides each retry, and the effect node starts each lane
through the remote-lane runner. This handler decides nothing: it passes each stage's answer to the
next and waits when the plan node says to wait.
"""

from __future__ import annotations

import time
from collections.abc import Callable

from omnimarket.models.merge_sweep import ModelMergeSweepReadRequest
from omnimarket.models.merge_sweep.model_merge_sweep_effect import (
    ModelMergeSweepLaneRunRequest,
    ModelMergeSweepLaneRunResult,
    ModelMergeSweepLoadRequest,
)
from omnimarket.models.merge_sweep.model_merge_sweep_plan import (
    ModelMergeSweepBriefRequest,
    ModelMergeSweepLane,
    ModelMergeSweepPlanRequest,
    ModelMergeSweepReading,
)
from omnimarket.models.merge_sweep.model_merge_sweep_retry import (
    ModelMergeSweepRetryRequest,
)

from ..models import (
    ModelMergeSweepLaneOutcome,
    ModelMergeSweepRetriedAfter,
    ModelMergeSweepRunRequest,
    ModelMergeSweepRunResult,
    ModelMergeSweepSupplement,
)
from ..protocols import ProtocolMergeSweepStages
from .handler_merge_sweep_stages_local import LocalMergeSweepStages


def supplemented(
    facts: ModelMergeSweepReadRequest, supplement: ModelMergeSweepSupplement | None
) -> ModelMergeSweepReadRequest:
    """Add the changed files and ready times a richer source than the watcher state supplied.

    A supplement is the caller's word that it read every open PR: a PR it does not list reads as
    having no changed files. A merge it does not list stays unread, and so does every fact when
    there is no supplement.
    """
    if supplement is None:
        return facts
    open_prs = [
        p.model_copy(
            update={
                "files": supplement.files.get(f"{p.repo}#{p.number}", []),
                "ready_at": supplement.ready_at.get(f"{p.repo}#{p.number}"),
                "facts_unread": [],
            }
        )
        for p in facts.open_prs
    ]
    merges = [
        m.model_copy(
            update={"files": supplement.merge_files.get(f"{m.repo}#{m.number}")}
        )
        for m in facts.merges
    ]
    return facts.model_copy(update={"open_prs": open_prs, "merges": merges})


class HandlerMergeSweepRun:
    """Run one merge sweep to its lanes' final receipts."""

    def __init__(
        self,
        stages: ProtocolMergeSweepStages | None = None,
        sleep: Callable[[float], None] | None = None,
    ) -> None:
        self._stages: ProtocolMergeSweepStages = (
            stages if stages is not None else LocalMergeSweepStages()
        )
        self._sleep = sleep if sleep is not None else time.sleep

    def handle(self, request: ModelMergeSweepRunRequest) -> ModelMergeSweepRunResult:
        loaded = self._stages.load_facts(
            ModelMergeSweepLoadRequest(
                state_path=request.state_path,
                ledger_path=request.ledger_path,
                ticks_path=request.ticks_path,
                floors_path=request.floors_path,
                now=request.now,
                load1=request.load1,
                cpus=request.cpus,
                window_min=request.window_min,
                max_reds=request.max_reds,
                max_age_s=request.max_age_s,
            )
        )
        if not loaded.ok or loaded.facts is None:
            return ModelMergeSweepRunResult(ok=False, why=loaded.why)
        reading = self._stages.read(supplemented(loaded.facts, request.supplement))
        plan = self._stages.plan(
            ModelMergeSweepPlanRequest(
                reading=ModelMergeSweepReading.model_validate(reading.model_dump()),
                max_lanes=request.max_lanes,
            )
        )
        outcomes = (
            [
                self._dispatch(request, lane, index)
                for index, lane in enumerate(plan.dispatch)
            ]
            if request.dispatch
            else []
        )
        return ModelMergeSweepRunResult(
            ok=True,
            complete=not reading.unread,
            unread=reading.unread,
            plan=plan,
            skipped=plan.skipped,
            deferred=plan.deferred,
            outcomes=outcomes,
        )

    def _start(
        self,
        request: ModelMergeSweepRunRequest,
        lane: ModelMergeSweepLane,
        name: str,
        base: str,
    ) -> ModelMergeSweepLaneRunResult:
        brief = self._stages.brief(
            ModelMergeSweepBriefRequest(
                lane=name,
                sweep_lane=request.lane,
                orchestrator=request.parent,
                ticket=request.ticket,
                kind=lane.kind,
                repo=lane.repo,
                prs=lane.prs,
                reasons=lane.reasons,
                **(
                    {"claim_check_command": request.claim_check_command}
                    if request.claim_check_command
                    else {}
                ),
            )
        )
        return self._stages.run_lane(
            ModelMergeSweepLaneRunRequest(
                runner_script=request.runner_script,
                brief_path=f"{request.brief_dir.rstrip('/')}/{base}.brief.md",
                brief_text=brief.text,
                lane=name,
                model=request.model,
                parent=request.lane,
                ticket=request.ticket,
                prs=[p.pr for p in lane.prs],
                repo=lane.repo,
                host=request.host,
            )
        )

    def _dispatch(
        self, request: ModelMergeSweepRunRequest, lane: ModelMergeSweepLane, index: int
    ) -> ModelMergeSweepLaneOutcome:
        name = f"{request.lane}-{lane.kind}-{index + 1}"
        events = ["start"]
        receipt = self._start(request, lane, name, name)
        waits, other_host_used = 0, False
        retried: ModelMergeSweepRetriedAfter | None = None
        while True:
            decision = self._stages.decide_retry(
                ModelMergeSweepRetryRequest(
                    exit_code=receipt.exit_code,
                    waits_used=waits,
                    host_retry_used=other_host_used,
                    retries=request.retries,
                    retry_wait_min=request.retry_wait_min,
                )
            )
            if decision.action == "wait_and_retry":
                events.append(f"wait:{decision.wait_min}")
                self._sleep(60.0 * (decision.wait_min or 0))
                waits += 1
                events.append("start")
                receipt = self._start(request, lane, name, name)
            elif decision.action == "retry_other_host":
                other_host_used = True
                retried = ModelMergeSweepRetriedAfter(
                    host=receipt.host, status=receipt.status, receipt=receipt.receipt
                )
                events.append("start-other-host")
                receipt = self._start(request, lane, f"{name}-r2", name)
            else:
                break
        return ModelMergeSweepLaneOutcome(
            lane=name,
            kind=lane.kind,
            repo=lane.repo,
            prs=[p.pr for p in lane.prs],
            exit_code=receipt.exit_code,
            status=receipt.status,
            host=receipt.host,
            retried_after=retried,
            events=events,
        )
