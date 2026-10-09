# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Definition-B decisions of the DoD closeout sweep (OMN-20675).

The dod-closeout-sweep workflow asked agents to make these decisions in prose: is this
date's sweep already delivered or owned by a peer, which sprint is live, which started
tickets are candidates and how they split into chunks. Here the caller performs every
read (ledger, Linear, GitHub, the report file) and passes what it saw; the handler
returns the decision. It reads nothing, has no clock and writes nothing.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta

from omnimarket.nodes.node_dod_closeout_sweep_compute.models.model_dod_closeout_sweep import (
    CLOCK_FORMAT,
    UUID_PATTERN,
    EnumCloseoutDecisionKind,
    EnumPrecheckVerdict,
    ModelDodCloseoutDecisionRequest,
    ModelDodCloseoutDecisionResult,
    ModelNonCandidate,
    ModelScopeCounts,
    ModelScopeTicket,
    ModelSprintProject,
)

LANE = "dod-closeout-sweep"
PEER_CLAIM_MINUTES = 180
REPORT_MAX_LINES = 150
# A TERMINAL row is a delivered pass only when it reports the counts.
REQUIRED_COUNT_CELLS = (
    "candidates",
    "flipped",
    "held",
    "merged_unreleased",
    "corrected",
    "reverted",
)
# The counts block of a conformant report names each of these.
COUNTS_BLOCK_TERMS = (
    "flipped",
    "held",
    "merged-unreleased",
    "corrected",
    "reverted",
    "fenced",
    "parents",
    "external",
)
COUNTS_BLOCK_WINDOW_LINES = 40

_SPRINT_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
_KEY_CELL = re.compile(r"^\s*([A-Za-z][\w-]*)=(.*)$", re.DOTALL)


def _parse_row(row: str) -> tuple[str, str, dict[str, str]] | None:
    """Timestamp, type and key=value cells of one ledger row; None for anything else."""
    cells = [c.strip() for c in row.split(" | ")]
    if len(cells) < 2:
        return None
    fields: dict[str, str] = {}
    for cell in cells[2:]:
        match = _KEY_CELL.match(cell)
        if match:
            fields.setdefault(match.group(1), match.group(2).strip())
    return cells[0], cells[1], fields


def _parse_ts(stamp: str) -> datetime | None:
    try:
        return datetime.strptime(stamp, CLOCK_FORMAT)
    except ValueError:
        return None


def report_conformance(report_text: str) -> tuple[int, list[str]]:
    """Line count and every failed conformance check of a closeout report."""
    lines = report_text.split("\n")
    if lines and lines[-1] == "":
        lines = lines[:-1]
    failed: list[str] = []
    if len(lines) > REPORT_MAX_LINES:
        failed.append(f"report-lines>{REPORT_MAX_LINES}")
    head = "\n".join(lines[:COUNTS_BLOCK_WINDOW_LINES]).lower().replace("_", "-")
    missing = [t for t in COUNTS_BLOCK_TERMS if t.replace("_", "-") not in head]
    if missing:
        failed.append("report-counts-block-missing:" + ",".join(missing))
    table_rows = [line.lower() for line in lines if line.lstrip().startswith("|")]
    if not any("merge" in r and "receipt" in r for r in table_rows):
        failed.append("report-flipped-table-missing")
    if not any("unmet" in r for r in table_rows):
        failed.append("report-held-table-missing")
    if not any("histogram" in line.lower() for line in lines):
        failed.append("report-histogram-missing")
    return len(lines), failed


def precheck(
    *,
    date: str,
    force: bool,
    clock_utc: str,
    ledger_rows: list[str],
    report_text: str | None,
    report_commit_sha: str,
    report_dirty: bool,
) -> ModelDodCloseoutDecisionResult:
    """Is this date's sweep delivered, owned by a live peer, or to be run.

    The fail direction is deliberate: anything unresolved is a full run, never a skip.
    """
    kind = EnumCloseoutDecisionKind.PRECHECK
    if force:
        return ModelDodCloseoutDecisionResult(
            kind=kind,
            verdict=EnumPrecheckVerdict.RUN,
            evidence="force=true",
            reason="force bypasses the idempotency guard and re-derives everything",
            checks_failed=[],
        )
    now = datetime.strptime(clock_utc, CLOCK_FORMAT)
    rows = [p for p in map(_parse_row, ledger_rows) if p and p[2].get("lane") == LANE]

    failed: list[str] = []
    report_lines: int | None = None
    if report_text is None:
        failed.append("report-missing")
    else:
        report_lines, conformance_failed = report_conformance(report_text)
        failed.extend(conformance_failed)
        if not report_commit_sha:
            failed.append("report-uncommitted")
        if report_dirty:
            failed.append("report-dirty")
    delivered_at = next(
        (
            ts
            for ts, row_type, fields in rows
            if row_type == "TERMINAL"
            and fields.get("date") == date
            and all(re.match(r"\d+\b", fields.get(c, "")) for c in REQUIRED_COUNT_CELLS)
        ),
        None,
    )
    if delivered_at is None:
        failed.append("ledger-terminal-missing")

    if not failed and delivered_at is not None:
        return ModelDodCloseoutDecisionResult(
            kind=kind,
            verdict=EnumPrecheckVerdict.ALREADY_DELIVERED,
            evidence=f"{report_commit_sha} {delivered_at}",
            reason=f"report committed and conformant, TERMINAL row {delivered_at} carries {date} and the counts",
            report_lines=report_lines,
            checks_failed=[],
        )

    terminals = [
        t
        for t in ((_parse_ts(r[0]), r[1]) for r in rows)
        if t[0] and t[1] == "TERMINAL"
    ]
    live: str | None = None
    stale: list[str] = []
    for ts_text, row_type, _fields in rows:
        ts = _parse_ts(ts_text)
        if row_type != "CLAIM" or ts is None:
            continue
        if any(t_ts is not None and t_ts >= ts for t_ts, _ in terminals):
            continue
        age = max(now - ts, timedelta(0))
        if age < timedelta(minutes=PEER_CLAIM_MINUTES):
            live = live or ts_text
        else:
            stale.append(ts_text)
    stale_note = (
        f"; stale CLAIM with no TERMINAL ignored: {', '.join(stale)}" if stale else ""
    )
    if live is not None:
        return ModelDodCloseoutDecisionResult(
            kind=kind,
            verdict=EnumPrecheckVerdict.PEER_OWNED,
            evidence=live,
            reason=f"CLAIM {live} has no TERMINAL and is under {PEER_CLAIM_MINUTES} minutes old{stale_note}",
            report_lines=report_lines,
            checks_failed=failed,
        )
    return ModelDodCloseoutDecisionResult(
        kind=kind,
        verdict=EnumPrecheckVerdict.RUN,
        evidence="; ".join(failed),
        reason=f"not delivered ({len(failed)} failed check(s)) and no live peer CLAIM{stale_note}",
        report_lines=report_lines,
        checks_failed=failed,
    )


def _dated_sprint(project: ModelSprintProject) -> bool:
    """A dated beta sprint: starts with Sprint, carries two ISO dates, ends with (Beta)."""
    name = project.name.strip()
    return (
        name.startswith("Sprint")
        and name.endswith("(Beta)")
        and len(_SPRINT_DATE.findall(name)) == 2
    )


def _window(project: ModelSprintProject) -> str:
    return (
        f"{project.start_date}..{project.target_date} "
        f"completedAt={project.completed_at or 'null'}"
    )


def resolve_sprint(
    *, date: str, project_override: str, projects: list[ModelSprintProject]
) -> ModelDodCloseoutDecisionResult:
    """The one live sprint whose window holds `date`; never a pick among several."""
    kind = EnumCloseoutDecisionKind.RESOLVE_SPRINT
    if project_override:
        if not UUID_PATTERN.match(project_override):
            raise ValueError(
                f"project override must be the 36-character uuid, got {project_override!r}"
            )
        match = next((p for p in projects if p.uuid == project_override), None)
        residuals = [
            "project given explicitly by the caller (override, not live resolution)"
        ]
        if match is None:
            residuals.append("the override is not among the listed projects")
        elif match.completed_at:
            residuals.append(
                f"the override is a completed sprint: {match.name} {_window(match)}"
            )
        return ModelDodCloseoutDecisionResult(
            kind=kind,
            resolved=True,
            project_id=project_override,
            project_name=match.name if match else None,
            residuals=residuals,
        )
    dated = [p for p in projects if _dated_sprint(p)]
    containing = [
        p
        for p in dated
        if p.start_date and p.target_date and p.start_date <= date <= p.target_date
    ]
    survivors = (
        [p for p in containing if not p.completed_at]
        if len(containing) > 1
        else containing
    )
    if len(survivors) == 1:
        only = survivors[0]
        return ModelDodCloseoutDecisionResult(
            kind=kind,
            resolved=True,
            project_id=only.uuid,
            project_name=f"{only.name} {_window(only)}",
            residuals=[],
        )
    considered = [
        f"{p.name} {_window(p)} contains-{date}={p in containing}" for p in dated
    ]
    return ModelDodCloseoutDecisionResult(
        kind=kind,
        resolved=False,
        project_id=None,
        project_name=None,
        residuals=[
            f"{len(survivors)} dated beta sprint(s) left for {date}; refusing to pick",
            *considered,
        ],
    )


def _why_not(ticket: ModelScopeTicket) -> str:
    reasons = []
    if ticket.has_children:
        reasons.append("parent (has children): the rollup owns its state")
    if ticket.fenced:
        reasons.append(f"fenced: {ticket.fence_reason or 'another lane holds it'}")
    if ticket.external:
        reasons.append("external collaborator: comment-only")
    if not ticket.occ_contract and ticket.merged_pr_count == 0:
        reasons.append("no OCC contract and no merged product PR")
    return "; ".join(reasons)


def plan_scope(
    *, project_id: str, tickets: list[ModelScopeTicket], chunk_size: int
) -> ModelDodCloseoutDecisionResult:
    """Classify started tickets by the four exclusions and cut the candidates into chunks."""
    kind = EnumCloseoutDecisionKind.PLAN_SCOPE
    if not UUID_PATTERN.match(project_id):
        raise ValueError(
            f"plan_scope needs the sprint's 36-character uuid, got {project_id!r}"
        )
    ids = [t.id for t in tickets]
    if len(set(ids)) != len(ids):
        raise ValueError(
            "plan_scope got a duplicate ticket id: "
            + ", ".join(sorted({i for i in ids if ids.count(i) > 1}))
        )
    not_started = [t.id for t in tickets if t.state_type != "started"]
    if not_started:
        raise ValueError(
            "plan_scope enumerates started tickets only; not started: "
            + ", ".join(not_started)
        )
    candidates: list[ModelScopeTicket] = []
    non_candidates: list[ModelNonCandidate] = []
    overlaps: list[str] = []
    for ticket in tickets:
        why = _why_not(ticket)
        if why:
            non_candidates.append(ModelNonCandidate(id=ticket.id, why_not=why))
            buckets = [
                name
                for name, hit in (
                    ("fenced", ticket.fenced),
                    ("parent", ticket.has_children),
                    ("external", ticket.external),
                    (
                        "no-contract-no-merged-pr",
                        not ticket.occ_contract and ticket.merged_pr_count == 0,
                    ),
                )
                if hit
            ]
            if len(buckets) > 1:
                overlaps.append(f"{ticket.id}: {' + '.join(buckets)}")
        else:
            candidates.append(ticket)
    names = [t.state_name.lower() for t in tickets]
    counts = ModelScopeCounts(
        enumerated=len(tickets),
        in_progress=names.count("in progress"),
        in_review=names.count("in review"),
        fenced=sum(t.fenced for t in tickets),
        parents=sum(t.has_children for t in tickets),
        external=sum(t.external for t in tickets),
        no_contract_no_merged_pr=sum(
            not t.occ_contract and t.merged_pr_count == 0 for t in tickets
        ),
        occ_contract_present=sum(t.occ_contract for t in tickets),
        candidates=len(candidates),
        candidates_with_occ_contract=sum(t.occ_contract for t in candidates),
    )
    candidate_ids = [t.id for t in candidates]
    return ModelDodCloseoutDecisionResult(
        kind=kind,
        counts=counts,
        candidates=candidate_ids,
        chunks=[
            candidate_ids[i : i + chunk_size]
            for i in range(0, len(candidate_ids), chunk_size)
        ],
        non_candidates=non_candidates,
        overlaps=overlaps,
        verified_nothing=not candidate_ids,
    )


class HandlerDodCloseoutSweep:
    """Pure decisions of the DoD closeout sweep. No reads, no clock, no writes."""

    def handle(
        self, request: ModelDodCloseoutDecisionRequest
    ) -> ModelDodCloseoutDecisionResult:
        kind = request.kind
        if kind is EnumCloseoutDecisionKind.PRECHECK:
            assert request.clock_utc is not None
            assert request.ledger_rows is not None
            return precheck(
                date=request.date,
                force=request.force,
                clock_utc=request.clock_utc,
                ledger_rows=request.ledger_rows,
                report_text=request.report_text,
                report_commit_sha=request.report_commit_sha,
                report_dirty=request.report_dirty,
            )
        if kind is EnumCloseoutDecisionKind.RESOLVE_SPRINT:
            assert request.projects is not None
            return resolve_sprint(
                date=request.date,
                project_override=request.project_override,
                projects=request.projects,
            )
        assert request.project_id is not None
        assert request.tickets is not None
        return plan_scope(
            project_id=request.project_id,
            tickets=request.tickets,
            chunk_size=request.chunk_size,
        )
