# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Enumerate the sprint tickets, defects and red or stalled PRs, each stage under its own floor (OMN-23250).

Moved from the frozen lab-fill script's enumerate (the Linear project read, the defect read, the
PR candidates from the PR watcher, the triage fences), with its verdicts unchanged: a PR is red when
the watcher's CI verdict for its head is RED, stalled when it has been idle longer than the bound,
and neither when the watcher holds no verdict for that head. A stage that is cut or unreadable
marks only itself; the candidates the others read are returned.
"""

from __future__ import annotations

import re
from collections.abc import Mapping

from ..models.model_lab_fill_sprint_tickets import (
    SPRINT_STAGES,
    ModelLabFillSprintCandidate,
    ModelLabFillSprintStage,
    ModelLabFillSprintTicketsRequest,
    ModelLabFillSprintTicketsResult,
    SprintStageState,
)
from ..protocols import LabFillPortError, LabFillSourceCutError, ProtocolLabFillClock
from ..protocols.protocol_lab_fill_enumerate_facts import (
    LabFillIssueNode,
    ProtocolLabFillFences,
    ProtocolLabFillSprintIssues,
    ProtocolLabFillWatcherPrs,
)
from .helpers_lab_fill_effect import default_clock

_TICKET = re.compile(r"OMN-[0-9]+")
_PR_KINDS = ("pr-red", "pr-stalled")
_HOLD_PREFIX = "hold:"
_HOLD_AUTO_MERGE = "hold:auto-merge"


def _detail(exc: Exception) -> str:
    return " ".join(str(exc).split())[:100]


def _candidate(
    node: LabFillIssueNode, kind: str, in_project: bool
) -> ModelLabFillSprintCandidate:
    return ModelLabFillSprintCandidate(
        kind=kind,
        ticket=node.ticket,
        title=node.title[:120],
        labels=node.labels,
        milestone=node.milestone,
        under_parent=in_project,
        updated_at=node.updated_at,
        project_id=node.project_id,
        assignee_id=node.assignee_id,
        ticket_prs=node.prs,
        merged_implementations=node.merged_implementations,
        ticket_readable=node.readable,
        declared_repo=node.description_repo,
    )


class HandlerLabFillSprintTickets:
    """Read the sprint project, the defects, the PR candidates and the fences."""

    def __init__(
        self,
        issues: ProtocolLabFillSprintIssues | None = None,
        watcher: ProtocolLabFillWatcherPrs | None = None,
        fences: ProtocolLabFillFences | None = None,
        clock: ProtocolLabFillClock | None = None,
    ) -> None:
        self._issues_port = issues
        self._watcher_port = watcher
        self._fences_port = fences
        self._clock = default_clock(clock)

    def _need(
        self,
    ) -> tuple[
        ProtocolLabFillSprintIssues, ProtocolLabFillWatcherPrs, ProtocolLabFillFences
    ]:
        if (
            self._issues_port is None
            or self._watcher_port is None
            or self._fences_port is None
        ):
            raise LabFillPortError(
                "enumerate_lab_fill_sprint_tickets: a reader is not injected on this host"
            )
        return self._issues_port, self._watcher_port, self._fences_port

    def handle(
        self, request: ModelLabFillSprintTicketsRequest
    ) -> ModelLabFillSprintTicketsResult:
        issues, watcher, fences = self._need()
        start = self._clock.now_epoch_s()
        stages: list[ModelLabFillSprintStage] = []
        diagnostics = dict.fromkeys(("ticket", "defect", *_PR_KINDS), "ok")

        def allowed(stage: str) -> float:
            behind = len(SPRINT_STAGES) - SPRINT_STAGES.index(stage) - 1
            left = request.budget_s - (self._clock.now_epoch_s() - start)
            return max(left - request.stage_floor_s * behind, request.stage_floor_s)

        def record(
            stage: str,
            state: SprintStageState,
            seen: int,
            detail: str,
            budget: float,
            began: float,
        ) -> None:
            stages.append(
                ModelLabFillSprintStage(
                    stage=stage,
                    state=state,
                    seen=seen,
                    detail=detail,
                    budget_s=budget,
                    elapsed_s=self._clock.now_epoch_s() - began,
                )
            )

        # project-issues
        budget, began = allowed("project-issues"), self._clock.now_epoch_s()
        tree: dict[str, LabFillIssueNode] = {}
        try:
            for node in issues.project(
                request.project_id, request.ticket_state_types, budget
            ):
                tree[node.ticket] = node
            record(
                "project-issues",
                "ok" if tree else "empty",
                len(tree),
                "",
                budget,
                began,
            )
        except (LabFillSourceCutError, LabFillPortError) as exc:
            state: SprintStageState = (
                "stale" if isinstance(exc, LabFillSourceCutError) else "unreadable"
            )
            diagnostics["ticket"] = f"{state}:project-issues"
            record("project-issues", state, 0, _detail(exc), budget, began)
        tickets = {
            t: _candidate(n, "ticket", True)
            for t, n in tree.items()
            if n.state_type in request.ticket_state_types and not n.has_children
        }

        # defects
        budget, began = allowed("defects"), self._clock.now_epoch_s()
        defects: dict[str, LabFillIssueNode] = {}
        if request.defect_labels or request.defect_terms:
            try:
                for node in issues.defects(
                    request.defect_labels, request.defect_terms, budget
                ):
                    defects[node.ticket] = node
                record(
                    "defects",
                    "ok" if defects else "empty",
                    len(defects),
                    "",
                    budget,
                    began,
                )
            except (LabFillSourceCutError, LabFillPortError) as exc:
                state = (
                    "stale" if isinstance(exc, LabFillSourceCutError) else "unreadable"
                )
                diagnostics["defect"] = f"{state}:defects"
                record("defects", state, 0, _detail(exc), budget, began)
        else:
            record(
                "defects",
                "empty",
                0,
                "no defect labels or terms declared",
                budget,
                began,
            )
        for ticket, node in defects.items():
            tickets[ticket] = _candidate(node, "defect", ticket in tree)

        # PR candidates from the watcher (a local read), then the assignment read of tickets outside the project
        pr_rows: list[
            tuple[str, str, str, str, str, tuple[str, ...], str, str, str]
        ] = []
        unknown: set[str] = set()
        try:
            for wp in watcher(request.pr_repos):
                found = _TICKET.findall(wp.title)
                if not found:
                    continue
                if not wp.head_read:
                    for kind in _PR_KINDS:
                        diagnostics[kind] = "partial:unreadable-head"
                    continue
                if not wp.ci_head_matches:
                    continue
                kind = (
                    "pr-red"
                    if wp.ci_verdict == "RED"
                    else "pr-stalled"
                    if wp.idle_hours > request.stalled_hours
                    else ""
                )
                if not kind:
                    continue
                ticket = found[0]
                if ticket not in tree and ticket not in tickets:
                    unknown.add(ticket)
                hold = next(
                    (
                        label
                        for label in wp.labels
                        if label.lower().startswith(_HOLD_PREFIX)
                        and label.lower() != _HOLD_AUTO_MERGE
                    ),
                    "",
                )
                pr_rows.append(
                    (
                        kind,
                        ticket,
                        f"{wp.repo}#{wp.number}",
                        wp.repo,
                        wp.head_ref,
                        wp.labels,
                        hold,
                        wp.title[:120],
                        wp.updated_at,
                    )
                )
        except LabFillPortError as exc:
            for kind in _PR_KINDS:
                diagnostics[kind] = "unreadable-from-snapshot"
            pr_rows = []
            unknown = set()
            watcher_detail = _detail(exc)
        else:
            watcher_detail = ""
        assignments: Mapping[str, LabFillIssueNode] = {}
        budget, began = allowed("pr-assignments"), self._clock.now_epoch_s()
        if unknown:
            try:
                assignments = issues.tickets(sorted(unknown), budget)
                record("pr-assignments", "ok", len(assignments), "", budget, began)
            except (LabFillSourceCutError, LabFillPortError) as exc:
                state = (
                    "stale" if isinstance(exc, LabFillSourceCutError) else "unreadable"
                )
                record("pr-assignments", state, 0, _detail(exc), budget, began)
        else:
            record("pr-assignments", "empty", 0, watcher_detail, budget, began)
        known = {**tree, **defects, **assignments}
        out = list(tickets.values())
        for (
            kind,
            ticket,
            key,
            repo,
            head_ref,
            labels,
            hold,
            title,
            updated_at,
        ) in pr_rows:
            issue = known.get(ticket)
            if issue is None:
                diagnostics[kind] = "partial:ticket-unreadable"
                continue
            out.append(
                ModelLabFillSprintCandidate(
                    kind=kind,
                    ticket=ticket,
                    title=title,
                    pr=key,
                    repo=repo,
                    head_ref=head_ref,
                    labels=labels,
                    hold_reason=hold,
                    under_parent=ticket in tree,
                    milestone=tree[ticket].milestone if ticket in tree else "",
                    updated_at=updated_at,
                    assignee_id=issue.assignee_id,
                )
            )

        # triage-fences
        budget, began = allowed("triage-fences"), self._clock.now_epoch_s()
        fenced: tuple[str, ...] = ()
        try:
            fenced = tuple(fences(budget))
            record(
                "triage-fences",
                "ok" if fenced else "empty",
                len(fenced),
                "",
                budget,
                began,
            )
        except (LabFillSourceCutError, LabFillPortError) as exc:
            state = "stale" if isinstance(exc, LabFillSourceCutError) else "unreadable"
            record("triage-fences", state, 0, _detail(exc), budget, began)
        return ModelLabFillSprintTicketsResult(
            stages=tuple(stages),
            candidates=tuple(out),
            diagnostics=diagnostics,
            fenced_tickets=fenced,
            stale=tuple(s.stage for s in stages if s.state == "stale"),
            unreadable=tuple(s.stage for s in stages if s.state == "unreadable"),
            elapsed_s=self._clock.now_epoch_s() - start,
        )
