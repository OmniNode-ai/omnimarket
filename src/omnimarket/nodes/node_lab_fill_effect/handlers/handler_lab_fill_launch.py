# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Launch: carry out one lane the plan node decided, inside its own share of the window (OMN-20668).

Decides nothing about host, engine or lane name. In order: the lane's share of the
window; the live checks for this lane; the brief (with an approved row's goal read
by id, then the committed standing rules); the runner's ``run --detach``. A lane
that cannot proceed returns ``detached=False`` with the reason; nothing is retried.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from pathlib import Path

from omnimarket.handlers.handler_internal_release_path_guard import (
    HandlerInternalReleasePathGuard,
)
from omnimarket.models.lab_fill import ModelLabFillApprovedRow, ModelLabFillLaunch
from omnimarket.models.model_internal_release_path_guard import (
    ModelInternalReleasePathGuardRequest,
)
from omnimarket.models.model_rule_premise import ModelBriefCheckRequest
from omnimarket.nodes.node_lab_placement_compute.handlers.handler_lab_lane_priority import (
    priority_class_args,
)
from omnimarket.nodes.node_lane_model_policy_compute.handlers.handler_lane_model_policy import (
    lane_model_refusal,
)
from omnimarket.nodes.node_rule_premise_effect import (
    BoundPremiseGate,
    receipt_allows,
)
from omnimarket.protocols.protocol_rule_premise import ProtocolRulePremiseGate

from ..models import (
    ModelLabFillLaunchRequest,
    ModelLabFillLaunchResult,
)
from ..models.model_lab_fill_send_gate import (
    EnumLabFillSendVerdict,
    ModelLabFillSendGateRequest,
)
from ..protocols import (
    LabFillPortError,
    ProtocolLabFillApprovedWork,
    ProtocolLabFillBriefBlocks,
    ProtocolLabFillClock,
    ProtocolLabFillLaneLauncher,
    ProtocolLabFillLiveChecks,
    ProtocolLabFillMergedWork,
)
from .handler_lab_fill_send_gate import HandlerLabFillSendGate
from .helpers_lab_fill_effect import default_clock, iso_utc

APPROVED_KINDS = ("process-fix", "partial-node", "wiring")
_DELEGATION_MARK = "3a.9 Delegation"
_RECEIPT = re.compile(r"receipt=(\S+)")
# The runner's own check of the brief, before anything is placed: a lab lane that mints a
# ticket, a time limit past the default with no reason line. The brief, not the host, is
# what is wrong, so the same brief is refused again.
_BRIEF_CHECK = re.compile(
    r"REMOTE-LANE-(?:(?:MINT|TIMEOUT)-REFUSED|BRIEF-UNCHECKED)\b.*"
)
_TIMED_OUT = 124


def runner_args(
    launch: ModelLabFillLaunch, brief_path: str, ticket_text: str = ""
) -> list[str]:
    """The arguments after the runner script, in the order the workflow built them.

    ``--host`` only for pinned work; ``--ref`` only with a repo. ``--placement auto``
    (OMN-21881) carries no ``--host``: a pinned host refuses a cloud session, so the
    runner picks the lab host and takes a cloud session only when none admits the lane.
    ``--priority-class`` is the class the ticket's ``Class:`` line names in ``ticket_text``, else absent
    (OMN-22122).
    """
    args = [
        "run",
        "--brief",
        brief_path,
        "--lane",
        launch.lane,
        "--ticket",
        launch.ticket,
        "--model",
        launch.engine,
        "--effort",
        launch.effort,
    ]
    if launch.placement:
        args += ["--placement", launch.placement]
    elif launch.host:
        args += ["--host", launch.host]
    args += ["--parent", launch.parent, "--timeout-min", str(launch.timeout_min)]
    if launch.repo:
        args += ["--repo", launch.repo]
        if launch.ref:
            args += ["--ref", launch.ref]
    if launch.pr:
        args += ["--pr", launch.pr]
    if launch.priority_class:
        args += ["--priority-class", launch.priority_class]
    else:
        args += priority_class_args(ticket_text)
    args.append("--detach")
    return args


def resolve_approved_row(
    brief: str, ref: ModelLabFillApprovedRow, row: Mapping[str, object]
) -> str:
    """Replace the brief's APPROVED-ROW marker line with the row's goal and acceptance check.

    Raises LabFillPortError (``APPROVED-ROW <id> <reason>``) when the row changed,
    is blocked, or the marker is not there exactly once.
    """

    def refuse(reason: str) -> LabFillPortError:
        return LabFillPortError(f"APPROVED-ROW {ref.id} {reason}")

    if row.get("kind") != ref.kind or row.get("ticket") != ref.ticket:
        raise refuse("kind or ticket changed")
    for key in ("goal", "acceptance_check"):
        value = row.get(key)
        if (
            not isinstance(value, str)
            or not value.strip()
            or "\n" in value
            or "\r" in value
        ):
            raise refuse(f"invalid {key}")
    if row.get("blocked_until"):
        raise refuse("blocked")
    lines = brief.splitlines(keepends=True)
    found = [i for i, line in enumerate(lines) if line.rstrip("\r\n") == ref.marker]
    if len(found) != 1:
        raise refuse("missing or duplicate marker")
    i = found[0]
    ending = lines[i][len(ref.marker) :]
    remaining = row.get("remaining")
    shipped = (
        "\nALREADY SHIPPED, REMAINS: " + remaining.strip()
        if isinstance(remaining, str) and remaining.strip()
        else ""
    )
    goal = str(row["goal"]).strip()
    check = str(row["acceptance_check"]).strip()
    lines[i] = f"GOAL: {goal}\nACCEPTANCE CHECK: {check}{shipped}{ending}"
    return "".join(lines)


class HandlerLabFillLaunch:
    """Write one lane's brief and detach it through the remote-lane runner."""

    def __init__(
        self,
        checks: ProtocolLabFillLiveChecks | None = None,
        approved: ProtocolLabFillApprovedWork | None = None,
        blocks: ProtocolLabFillBriefBlocks | None = None,
        runner: ProtocolLabFillLaneLauncher | None = None,
        clock: ProtocolLabFillClock | None = None,
        premise_gate: ProtocolRulePremiseGate | None = None,
        merged: ProtocolLabFillMergedWork | None = None,
    ) -> None:
        from ..protocols.local_lab_fill_adapters import (
            LocalApprovedWork,
            LocalBriefBlocks,
            LocalLaneLauncher,
            LocalLiveChecks,
            LocalMergedWork,
        )

        self._merged = merged if merged is not None else LocalMergedWork()

        self._checks = checks if checks is not None else LocalLiveChecks()
        self._approved = approved if approved is not None else LocalApprovedWork()
        self._blocks = blocks if blocks is not None else LocalBriefBlocks()
        self._runner = runner if runner is not None else LocalLaneLauncher()
        self._clock = default_clock(clock)
        self._premise_gate = (
            premise_gate if premise_gate is not None else BoundPremiseGate()
        )

    def handle(self, request: ModelLabFillLaunchRequest) -> ModelLabFillLaunchResult:
        started = self._clock.now_epoch_s()
        launch = request.launch

        def result(
            *,
            detached: bool,
            receipt: str = "",
            skipped: str = "",
            brief_has_delegation: bool = False,
            detail: str = "",
            elapsed_s: int | None = None,
            routed_to: str = "",
            merged_prs: tuple[str, ...] = (),
        ) -> ModelLabFillLaunchResult:
            return ModelLabFillLaunchResult(
                lane=launch.lane,
                detached=detached,
                receipt=receipt,
                skipped=skipped,
                brief_has_delegation=brief_has_delegation,
                detail=detail,
                elapsed_s=(
                    elapsed_s
                    if elapsed_s is not None
                    else int(self._clock.now_epoch_s() - started)
                ),
                routed_to=routed_to,
                merged_prs=merged_prs,
            )

        if refusal := lane_model_refusal(launch.engine):
            return result(
                detached=False, skipped="model-policy-refused", detail=refusal
            )

        slice_s = self._slice(request, started)
        if slice_s < request.min_lane_s:
            return result(
                detached=False, skipped="deadline", detail=iso_utc(started), elapsed_s=0
            )

        try:
            reason, detail = self._checks.skip_reason(
                ticket=launch.ticket,
                kind=request.kind,
                pr=launch.pr or "",
                operator_id=request.operator_id,
                ledger_path=request.ledger_path,
            )
        except LabFillPortError as exc:
            reason, detail = "ticket-unreadable", str(exc)
        if reason:
            return result(detached=False, skipped=reason, detail=detail)
        if self._clock.now_epoch_s() - started >= slice_s:
            return result(detached=False, skipped=f"lane-timeout:{int(slice_s)}s")

        brief_path = str(Path(request.brief_dir) / f"{launch.lane}.md")
        brief = request.brief
        if launch.approved_row is not None:
            try:
                row = self._approved.row(
                    request.approved_work_path, launch.approved_row.id
                )
                brief = resolve_approved_row(brief, launch.approved_row, row)
            except LabFillPortError as exc:
                return result(
                    detached=False,
                    skipped="approved-row-unresolved",
                    detail=str(exc)[:300],
                )
        try:
            merged_prs = tuple(
                self._merged.merged_prs(launch.ticket, request.watcher_state_path)
            )
            built = tuple(
                self._merged.built_reports(launch.ticket, request.ledger_path)
            )
        except LabFillPortError as exc:
            # An unread merge state is not a clear one: a lane sent blind may redo merged work.
            return result(
                detached=False, skipped="merged-state-unreadable", detail=str(exc)[:300]
            )
        gate = HandlerLabFillSendGate().handle(
            ModelLabFillSendGateRequest(
                ticket=launch.ticket,
                kind=request.kind,
                brief=brief,
                host=launch.host,
                merged_prs=merged_prs,
                built=built,
                exempt_kinds=request.closer_exempt_kinds,
                fact_hosts=request.fact_hosts,
                needs_hosts=request.needs_hosts,
                host_reach=request.host_reach,
            )
        )
        if gate.verdict is not EnumLabFillSendVerdict.SEND:
            return result(
                detached=False,
                skipped=gate.verdict.value,
                detail=gate.detail[:300],
                routed_to=gate.routed_to,
                merged_prs=gate.merged_prs,
            )
        if gate.host and gate.host != launch.host:
            launch = launch.model_copy(update={"host": gate.host, "placement": None})
        guard = HandlerInternalReleasePathGuard().handle(
            ModelInternalReleasePathGuardRequest(
                proposed_actions=brief,
                consumer_scope=request.dependency_consumer_scope,
                dependency_strategy=request.dependency_strategy,
            )
        )
        if not guard.allowed:
            return result(
                detached=False,
                skipped="internal-release-path",
                detail=guard.remediation,
            )
        try:
            Path(request.brief_dir).mkdir(parents=True, exist_ok=True)
            Path(brief_path).write_text(brief, encoding="utf-8")
            tail = self._blocks.blocks(ticket=launch.ticket, brief_path=brief_path)
        except (LabFillPortError, OSError) as exc:
            return result(
                detached=False, skipped="brief-rules-unreadable", detail=str(exc)[:300]
            )
        full = brief.rstrip("\n") + "\n\n" + tail
        Path(brief_path).write_text(full, encoding="utf-8")
        has_delegation = _DELEGATION_MARK in full

        checked = self._premise_gate.handle(
            ModelBriefCheckRequest(
                brief=full,
                lane=launch.lane,
                ticket=launch.ticket,
                host=launch.host or launch.placement or "",
                repository=launch.repo or "",
                now=self._clock.now_epoch_s(),
                batch=request.batch,
            )
        )
        if not receipt_allows(full, checked, require_admission=True):
            return result(
                detached=False,
                skipped="brief-check-refused",
                detail=checked.reason or "missing brief-check receipt",
                brief_has_delegation=has_delegation,
            )
        try:
            unchanged = Path(brief_path).read_bytes() == full.encode("utf-8")
        except OSError:
            unchanged = False
        if not unchanged:
            return result(
                detached=False,
                skipped="brief-check-refused",
                detail="brief changed after its check",
                brief_has_delegation=has_delegation,
            )

        left = slice_s - (self._clock.now_epoch_s() - started)
        if left <= 0:
            return result(
                detached=False,
                skipped=f"lane-timeout:{int(slice_s)}s",
                brief_has_delegation=has_delegation,
            )
        try:
            outcome = self._runner.run(
                runner_args(launch, brief_path, full),
                env={
                    "ONEX_LEDGER_PATH": request.ledger_path,
                    "ONEX_BRIEF_CHECK_RECEIPT": checked.receipt,
                    **(
                        {
                            "ONEX_BATCH_SLOT": checked.slot,
                            "ONEX_BATCH_ATTEMPT": str(checked.attempt),
                        }
                        if checked.slot
                        else {}
                    ),
                },
                timeout_s=left,
            )
        except LabFillPortError as exc:
            return result(
                detached=False,
                skipped="runner-unavailable",
                brief_has_delegation=has_delegation,
                detail=str(exc)[:300],
            )
        if outcome.returncode == _TIMED_OUT:
            used = int(self._clock.now_epoch_s() - started)
            return result(
                detached=False,
                skipped=f"lane-timeout:{used}s",
                brief_has_delegation=has_delegation,
            )
        lines = [ln for ln in outcome.stdout.splitlines() if ln.strip()]
        detached_line = next((ln for ln in lines if "DETACHED" in ln), "")
        match = _RECEIPT.search(detached_line)
        if outcome.returncode == 0 and match:
            return result(
                detached=True,
                receipt=match.group(1),
                brief_has_delegation=has_delegation,
                detail=detached_line[:300],
            )
        refused = next(
            (
                m.group(0)
                for ln in (*lines, *outcome.stderr.splitlines())
                if (m := _BRIEF_CHECK.search(ln))
            ),
            "",
        )
        if refused:
            return result(
                detached=False,
                skipped="brief-check-refused",
                brief_has_delegation=has_delegation,
                detail=refused[:300],
            )
        tail_lines = lines[-2:] or [outcome.stderr.strip()[-200:]]
        return result(
            detached=False,
            brief_has_delegation=has_delegation,
            detail=" / ".join(tail_lines)[:300],
        )

    def _slice(self, request: ModelLabFillLaunchRequest, now: float) -> float:
        """This lane's share of what is left of the window: seconds left over lanes still to go."""
        if request.window_end_epoch_s is None:
            return float(request.default_slice_s)
        left = max(0.0, request.window_end_epoch_s - now)
        return left / request.lanes_left
