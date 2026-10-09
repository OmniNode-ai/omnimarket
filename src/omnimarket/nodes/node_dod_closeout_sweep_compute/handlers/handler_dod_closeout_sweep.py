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
from collections import Counter
from datetime import datetime, timedelta

from omnimarket.nodes.node_dod_closeout_sweep_compute.handlers.closer_binding_rules import (
    refusal_of,
    refusal_reason,
    same_actor,
)
from omnimarket.nodes.node_dod_closeout_sweep_compute.handlers.closer_ticket_rules import (
    chunk_list,
    comment_signature,
    decide_ticket,
    open_comment,
    read_dod_verify,
    select_candidates,
)
from omnimarket.nodes.node_dod_closeout_sweep_compute.models.model_dod_closeout_sweep import (
    CLOCK_FORMAT,
    UUID_PATTERN,
    EnumCloseoutAction,
    EnumCloseoutDecisionKind,
    EnumPrecheckVerdict,
    EnumReleasedState,
    EnumTicketDecision,
    ModelChunkResult,
    ModelCloseoutCounts,
    ModelDodCloseoutDecisionRequest,
    ModelDodCloseoutDecisionResult,
    ModelHistogramRow,
    ModelNonCandidate,
    ModelScopeCounts,
    ModelScopeTicket,
    ModelSprintProject,
    ModelTicketResult,
)

LANE = "dod-closeout-sweep"
REPORT_PATH_TEMPLATE = "beta/tracking/{date}-dod-closeout-sweep.md"
# Blocking classes of a flip, in the order the primary blocker is chosen: a ticket whose
# work is not merged is held for that before its evidence is weighed.
BLOCK_PR_UNMERGED = "pr:unmerged"
BLOCK_AC_UNPARSEABLE = "ac:unparseable"
BLOCK_AC_UNBOUND = "ac:unbound"
BLOCK_CHECKS_TALLY = "checks:tally"
BLOCK_BEHAVIOR_NONE = "behavior:none"
BLOCK_REVERTED_UNCHANGED = "reverted:unchanged"
BLOCK_MERGED_UNRELEASED = "released:merged-unreleased"
BLOCK_RELEASED_INDETERMINATE = "released:indeterminate"
BLOCK_UNCLASSIFIED = "unclassified"
BLOCK_EXTERNAL = "external:comment-only"
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

# Sprints named from 2026-09-28 on do not zero-pad: "Sprint 2026-10-5 -> 2026-10-11 ...".
_SPRINT_DATE = re.compile(r"\d{4}-\d{1,2}-\d{1,2}")
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
    """A dated sprint: the name starts with Sprint and carries two dates, whatever follows.

    The window is read from the project's own start and target dates, never from the name.
    """
    name = project.name.strip()
    return name.startswith("Sprint") and len(_SPRINT_DATE.findall(name)) == 2


def _window(project: ModelSprintProject) -> str:
    return (
        f"{project.start_date}..{project.target_date} "
        f"completedAt={project.completed_at or 'null'}"
    )


def _previous_sprint(
    live: ModelSprintProject, dated: list[ModelSprintProject]
) -> tuple[str, str, list[str]]:
    """The dated sprint other than `live` ending latest on or before `live` starts.

    Its started tickets stay in scope until the sprint roll moves them, completed or not.
    Returns its uuid and name with window, or both uuids and both names when two share
    that latest end, and the residuals saying so. Empty strings when there is none.
    """
    start = live.start_date
    ended = [
        p
        for p in dated
        if p.uuid != live.uuid and start and p.target_date and p.target_date <= start
    ]
    if not ended:
        return "", "", []
    latest = max(p.target_date or "" for p in ended)
    tied = [p for p in ended if p.target_date == latest]
    residuals = (
        [
            f"{len(tied)} previous sprints share the latest end {latest}; both are returned"
        ]
        if len(tied) > 1
        else []
    )
    return (
        ",".join(p.uuid for p in tied),
        "; ".join(f"{p.name} {_window(p)}" for p in tied),
        residuals,
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
        previous_id, previous_name, previous_residuals = _previous_sprint(only, dated)
        return ModelDodCloseoutDecisionResult(
            kind=kind,
            resolved=True,
            project_id=only.uuid,
            project_name=f"{only.name} {_window(only)}",
            previous_project_id=previous_id,
            previous_project_name=previous_name,
            residuals=previous_residuals,
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
            f"{len(survivors)} dated sprint(s) left for {date}; refusing to pick",
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


def flip_decision(
    *, request: ModelDodCloseoutDecisionRequest
) -> ModelDodCloseoutDecisionResult:
    """Apply the flip predicate to one ticket's verifier counters.

    A flip needs every part: every cited PR merged; at least one parsed acceptance
    criterion and every one bound to a verified probative check; the tally
    verified + non-probative == total over a non-empty check set; at least one
    behaviour-proving check; and no prior reversal without a changed outcome. Then the
    release state: merged-unreleased and indeterminate are holds of their own, never a flip.
    """
    kind = EnumCloseoutDecisionKind.FLIP_DECISION
    assert request.total_checks is not None
    unbound = [c for c in request.criteria if c not in set(request.bound_criteria)]
    unmet: list[str] = []
    if not request.all_prs_merged:
        unmet.append(BLOCK_PR_UNMERGED)
    if not request.criteria:
        unmet.append(BLOCK_AC_UNPARSEABLE)
    elif unbound:
        unmet.append(BLOCK_AC_UNBOUND)
    if (
        request.total_checks == 0
        or request.verified_count + request.non_probative_count != request.total_checks
    ):
        unmet.append(BLOCK_CHECKS_TALLY)
    if request.behavior_proving_count == 0:
        unmet.append(BLOCK_BEHAVIOR_NONE)
    if request.prior_reversal and not request.outcome_changed_since_reversal:
        unmet.append(BLOCK_REVERTED_UNCHANGED)
    if unmet:
        return ModelDodCloseoutDecisionResult(
            kind=kind,
            action=EnumCloseoutAction.HELD_GAP,
            predicate_met=False,
            unmet=unmet,
            primary_blocker=unmet[0],
            unbound_criteria=unbound,
        )
    if request.released is EnumReleasedState.MERGED_UNRELEASED:
        return ModelDodCloseoutDecisionResult(
            kind=kind,
            action=EnumCloseoutAction.HELD_MERGED_UNRELEASED,
            predicate_met=True,
            unmet=[BLOCK_MERGED_UNRELEASED],
            primary_blocker=BLOCK_MERGED_UNRELEASED,
            unbound_criteria=[],
        )
    if request.released is EnumReleasedState.INDETERMINATE:
        return ModelDodCloseoutDecisionResult(
            kind=kind,
            action=EnumCloseoutAction.HELD_GAP,
            predicate_met=True,
            unmet=[BLOCK_RELEASED_INDETERMINATE],
            primary_blocker=BLOCK_RELEASED_INDETERMINATE,
            unbound_criteria=[],
        )
    return ModelDodCloseoutDecisionResult(
        kind=kind,
        action=EnumCloseoutAction.FLIPPED_DONE,
        predicate_met=True,
        unmet=[],
        primary_blocker=None,
        unbound_criteria=[],
    )


def _cell(text: str) -> str:
    """One markdown table cell: no pipe, no line break."""
    return " ".join(text.replace("|", "/").split()) or "-"


def _table(header: list[str], rows: list[list[str]], *, empty: str) -> list[str]:
    if not rows:
        return [empty]
    return [
        "| " + " | ".join(header) + " |",
        "|" + "---|" * len(header),
        *["| " + " | ".join(_cell(c) for c in row) + " |" for row in rows],
    ]


def _histogram(counter: Counter[str]) -> list[ModelHistogramRow]:
    return [
        ModelHistogramRow(check_class=name, count=count)
        for name, count in sorted(counter.items(), key=lambda kv: (-kv[1], kv[0]))
    ]


def report(
    *, request: ModelDodCloseoutDecisionRequest
) -> ModelDodCloseoutDecisionResult:
    """Counts, histogram, report text and TERMINAL cells from the chunk results.

    Every count is derived from the per-ticket results, never from a figure a chunk
    reported about itself. A dropped chunk is named, and so is every candidate no chunk
    adjudicated: an unadjudicated ticket must not read as one with no findings.
    """
    kind = EnumCloseoutDecisionKind.REPORT
    assert request.scope_counts is not None
    assert request.chunk_results is not None
    assert request.project_id is not None
    scope = request.scope_counts
    date = request.date
    chunks: list[ModelChunkResult] = sorted(
        request.chunk_results, key=lambda c: c.chunk
    )
    results: list[ModelTicketResult] = [r for c in chunks for r in c.results]
    reverted = sorted({t for c in chunks for t in c.reverted})
    dropped = [c.chunk for c in chunks if c.dropped]
    adjudicated = {r.id for r in results}
    unadjudicated = [t for c in chunks for t in c.tickets if t not in adjudicated]
    flipped = [
        r
        for r in results
        if r.action is EnumCloseoutAction.FLIPPED_DONE and r.id not in reverted
    ]
    held = [
        r
        for r in results
        if r.action in (EnumCloseoutAction.HELD_GAP, EnumCloseoutAction.HELD_EXTERNAL)
    ]
    unreleased = [
        r for r in results if r.action is EnumCloseoutAction.HELD_MERGED_UNRELEASED
    ]
    corrected = [r for r in results if r.action is EnumCloseoutAction.CORRECTED_STATE]
    counts = ModelCloseoutCounts(
        enumerated=scope.enumerated,
        candidates=scope.candidates,
        flipped=len(flipped),
        held=len(held),
        merged_unreleased=len(unreleased),
        corrected=len(corrected),
        reverted=len(reverted),
        fenced=scope.fenced,
        parents=scope.parents,
        external=scope.external,
    )
    primary: Counter[str] = Counter(
        r.primary_blocker
        or (
            BLOCK_EXTERNAL
            if r.action is EnumCloseoutAction.HELD_EXTERNAL
            else BLOCK_UNCLASSIFIED
        )
        for r in held
    )
    tooling: Counter[str] = Counter()
    for r in results:
        classes = set(r.tooling_blockers)
        if r.action is EnumCloseoutAction.HELD_MERGED_UNRELEASED:
            classes.add(BLOCK_MERGED_UNRELEASED)
        if r.primary_blocker == BLOCK_RELEASED_INDETERMINATE:
            classes.add(BLOCK_RELEASED_INDETERMINATE)
        tooling.update(classes)
    known_behavior = [r for r in results if r.behavior_proving_count is not None]
    behavior_positive = sum((r.behavior_proving_count or 0) > 0 for r in known_behavior)
    predicate_satisfied = sum(r.predicate_met is True for r in results)
    path = REPORT_PATH_TEMPLATE.format(date=date)
    verified_nothing = scope.candidates == 0

    lines: list[str] = []
    if verified_nothing:
        lines += [
            "VERIFIED NOTHING: zero candidates",
            f"Sprint read: {request.project_id} {request.project_name}".rstrip(),
            f"Exclusions of {scope.enumerated} enumerated: fenced {scope.fenced}, parents {scope.parents}, external {scope.external}, no contract and no merged PR {scope.no_contract_no_merged_pr}",
            "",
        ]
    lines += [
        f"# DoD closeout sweep {date}",
        f"Sprint: {request.project_name or '-'} ({request.project_id}) | mode: {'apply' if request.apply else 'dry (no state written)'}",
        "",
        "## 1. Counts",
        *_table(
            [
                "flipped",
                "held",
                "merged-unreleased",
                "corrected",
                "reverted",
                "fenced",
                "parents",
                "external",
                "candidates",
                "enumerated",
            ],
            [
                [
                    str(n)
                    for n in (
                        counts.flipped,
                        counts.held,
                        counts.merged_unreleased,
                        counts.corrected,
                        counts.reverted,
                        counts.fenced,
                        counts.parents,
                        counts.external,
                        counts.candidates,
                        counts.enumerated,
                    )
                ]
            ],
            empty="",
        ),
        "Buckets overlap: a ticket can be fenced and a parent. merged-unreleased is disjoint from held.",
        f"OCC contracts: {scope.occ_contract_present} of {scope.enumerated} enumerated, {scope.candidates_with_occ_contract} of {scope.candidates} candidates.",
    ]
    if dropped or unadjudicated:
        lines += [
            "",
            "## DROPPED CHUNKS",
            f"Chunks {', '.join(map(str, dropped)) or 'none'} returned nothing. Not adjudicated: {', '.join(unadjudicated) or 'none'}.",
        ]
    lines += [
        "",
        "## 2. Flipped tickets",
        *_table(
            ["ticket", "PR", "merge sha", "receipt"],
            [
                [
                    r.id,
                    r.product_pr or "-",
                    r.merge_sha or "-",
                    r.receipt or "NONE CITED - defect",
                ]
                for r in flipped
            ],
            empty="| ticket | PR | merge sha | receipt |\n|---|---|---|---|\n| none | - | - | - |",
        ),
        "",
        "## 3. Held tickets",
        *_table(
            ["ticket", "unmet check"],
            [
                [r.id, r.unmet_check or r.primary_blocker or r.evidence or "-"]
                for r in held
            ],
            empty="| ticket | unmet check |\n|---|---|\n| none | - |",
        ),
        "",
        "## 3b. Merged-unreleased tickets",
        *_table(
            [
                "ticket",
                "repo",
                "merge sha",
                "tag lookup",
                "index read",
                "release ticket",
            ],
            [
                [
                    r.id,
                    r.repo,
                    r.merge_sha,
                    r.tag_lookup,
                    r.index_read,
                    r.release_ticket or "no release ticket exists",
                ]
                for r in unreleased
            ],
            empty=(
                "None. Positive control: " + request.released_positive_control
                if unreleased == []
                and request.released_probe_run
                and request.released_positive_control
                else "None, but the released probe was not run or carries no positive control: an unrun probe is not a zero."
            ),
        ),
        "",
        "## 4. Blocking-check-class histogram",
        "### 4a. Primary DoD blocker per held ticket (sums to held)",
        *_table(
            ["class", "tickets"],
            [[h.check_class, str(h.count)] for h in _histogram(primary)],
            empty="| class | tickets |\n|---|---|\n| none | 0 |",
        ),
        "### 4b. Verifier and tooling blockers (occurrences, not a partition)",
        *_table(
            ["class", "occurrences"],
            [[h.check_class, str(h.count)] for h in _histogram(tooling)],
            empty="| class | occurrences |\n|---|---|\n| none | 0 |",
        ),
        f"behavior_proving_count > 0: {behavior_positive} of {len(known_behavior)} with a reading; full flip predicate satisfied: {predicate_satisfied} of {len(results)}.",
        f"ac:unbound {primary.get(BLOCK_AC_UNBOUND, 0)}, ac:unparseable {primary.get(BLOCK_AC_UNPARSEABLE, 0)} as the primary blocker.",
        "",
        "## 5. Plan comparison",
        request.plan_comparison or "NOT PROVIDED",
        "",
        "## 6. Audit",
        *[
            f"- chunk {c.chunk}: "
            + (
                "DROPPED, not adjudicated"
                if c.dropped
                else ("audited" if c.audited else "NOT AUDITED")
            )
            + f"; reverted: {', '.join(c.reverted) or 'none'}"
            + (f"; {_cell(c.audit_notes)}" if c.audit_notes else "")
            for c in chunks
        ],
        "",
        "## 7. Not done",
        request.not_done or "NOT PROVIDED",
    ]
    text = "\n".join(lines) + "\n"
    line_count, conformance_failed = report_conformance(text)
    cite = f" [cite: {path}"
    terminal_cells = [
        f"lane={LANE}",
        f"date={date}",
        f"friction={request.friction}",
        f"sprint={request.project_id}",
        f"enumerated={counts.enumerated}{cite} §1]",
        f"candidates={counts.candidates}{cite} §1]",
        f"flipped={counts.flipped}{cite} §2]",
        f"held={counts.held}{cite} §3]",
        f"merged_unreleased={counts.merged_unreleased}{cite} §3b]",
        f"corrected={counts.corrected}{cite} §1]",
        f"reverted={counts.reverted}{cite} §6]",
        f"fenced={counts.fenced}{cite} §1]",
        f"parents={counts.parents}{cite} §1]",
        f"external={counts.external}{cite} §1]",
    ]
    return ModelDodCloseoutDecisionResult(
        kind=kind,
        report_path=path,
        report_text=text,
        report_lines=line_count,
        checks_failed=conformance_failed,
        closeout_counts=counts,
        primary_histogram=_histogram(primary),
        tooling_histogram=_histogram(tooling),
        behavior_proving_positive=behavior_positive,
        predicate_satisfied=predicate_satisfied,
        dropped_chunks=dropped,
        unadjudicated_tickets=unadjudicated,
        verified_nothing=verified_nothing,
        terminal_cells=terminal_cells,
    )


def refuse_binding(
    *, request: ModelDodCloseoutDecisionRequest
) -> ModelDodCloseoutDecisionResult:
    """Whether the binder's check may go to the acceptor; the criterion label stands in for a
    check that carries none."""
    check = request.check
    if check is not None and not check.label and request.criterion_label:
        check = check.model_copy(update={"label": request.criterion_label})
    refusal = refusal_of(check)
    return ModelDodCloseoutDecisionResult(
        kind=EnumCloseoutDecisionKind.REFUSE_BINDING,
        refused=refusal is not None,
        refusal_class=refusal,
        reason=None if refusal is None else refusal_reason(refusal),
    )


def rotate_candidates(
    *, request: ModelDodCloseoutDecisionRequest
) -> ModelDodCloseoutDecisionResult:
    """The tickets a bounded run examines, least recently examined first, cut into chunks."""
    assert request.records is not None
    assert request.max_candidates is not None
    selected, deferred, held = select_candidates(
        records=request.records,
        text_state=request.text_state,
        max_candidates=request.max_candidates,
    )
    return ModelDodCloseoutDecisionResult(
        kind=EnumCloseoutDecisionKind.SELECT_CANDIDATES,
        selected=selected,
        deferred=deferred,
        held=held,
        chunks=chunk_list(selected, request.chunk_size),
    )


def judge_ticket(
    *, request: ModelDodCloseoutDecisionRequest
) -> ModelDodCloseoutDecisionResult:
    """Done only on every criterion bound, accepted by another lane and verified; else open
    with the unmet criteria and the comment the ticket receives."""
    assert request.ticket_id is not None
    assert request.acs is not None
    dod_verify = (
        {}
        if request.dod_verify_receipt is None
        else read_dod_verify(request.dod_verify_receipt)
    )
    decision, unmet = decide_ticket(
        acs=request.acs,
        dod_verify=dod_verify,
        criteria_note=request.criteria_note,
        criteria_amendment=request.criteria_amendment,
    )
    opened = decision is EnumTicketDecision.OPEN
    needs_amendment = any(u.amendment for u in unmet)
    sha = request.text_sha if needs_amendment else ""
    return ModelDodCloseoutDecisionResult(
        kind=EnumCloseoutDecisionKind.DECIDE_TICKET,
        decision=decision,
        ticket_unmet=unmet,
        needs_amendment=needs_amendment,
        signature=f"signature={comment_signature(unmet, sha)}" if opened else None,
        comment_text=(
            open_comment(request.ticket_id, request.run_key, unmet, sha)
            if opened
            else None
        ),
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
        if kind is EnumCloseoutDecisionKind.FLIP_DECISION:
            return flip_decision(request=request)
        if kind is EnumCloseoutDecisionKind.REPORT:
            return report(request=request)
        if kind is EnumCloseoutDecisionKind.REFUSE_BINDING:
            return refuse_binding(request=request)
        if kind is EnumCloseoutDecisionKind.CHECK_IDENTITIES:
            return ModelDodCloseoutDecisionResult(
                kind=kind,
                same_actor=same_actor(request.proposed_by, request.accepted_by),
            )
        if kind is EnumCloseoutDecisionKind.SELECT_CANDIDATES:
            return rotate_candidates(request=request)
        if kind is EnumCloseoutDecisionKind.DECIDE_TICKET:
            return judge_ticket(request=request)
        assert request.project_id is not None
        assert request.tickets is not None
        return plan_scope(
            project_id=request.project_id,
            tickets=request.tickets,
            chunk_size=request.chunk_size,
        )
