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

from omnimarket.models.lab_fill import ModelLabFillApprovedRow, ModelLabFillLaunch

from ..models import (
    ModelLabFillLaunchRequest,
    ModelLabFillLaunchResult,
)
from ..protocols import (
    LabFillPortError,
    ProtocolLabFillApprovedWork,
    ProtocolLabFillBriefBlocks,
    ProtocolLabFillClock,
    ProtocolLabFillLaneLauncher,
    ProtocolLabFillLiveChecks,
)
from .helpers_lab_fill_effect import default_clock, iso_utc

APPROVED_KINDS = ("process-fix", "partial-node", "wiring")
_DELEGATION_MARK = "3a.9 Delegation"
_RECEIPT = re.compile(r"receipt=(\S+)")
_TIMED_OUT = 124


def runner_args(launch: ModelLabFillLaunch, brief_path: str) -> list[str]:
    """The arguments after the runner script, in the order the workflow built them.

    ``--host`` only for pinned work; ``--ref`` only with a repo.
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
    if launch.host:
        args += ["--host", launch.host]
    args += ["--parent", launch.parent, "--timeout-min", str(launch.timeout_min)]
    if launch.repo:
        args += ["--repo", launch.repo]
        if launch.ref:
            args += ["--ref", launch.ref]
    if launch.pr:
        args += ["--pr", launch.pr]
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
    ) -> None:
        from ..protocols.local_lab_fill_adapters import (
            LocalApprovedWork,
            LocalBriefBlocks,
            LocalLaneLauncher,
            LocalLiveChecks,
        )

        self._checks = checks if checks is not None else LocalLiveChecks()
        self._approved = approved if approved is not None else LocalApprovedWork()
        self._blocks = blocks if blocks is not None else LocalBriefBlocks()
        self._runner = runner if runner is not None else LocalLaneLauncher()
        self._clock = default_clock(clock)

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

        left = slice_s - (self._clock.now_epoch_s() - started)
        if left <= 0:
            return result(
                detached=False,
                skipped=f"lane-timeout:{int(slice_s)}s",
                brief_has_delegation=has_delegation,
            )
        try:
            outcome = self._runner.run(
                runner_args(launch, brief_path),
                env={"ONEX_LEDGER_PATH": request.ledger_path},
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
