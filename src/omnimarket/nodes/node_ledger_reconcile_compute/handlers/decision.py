# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The reconciliation decision: ledger text and verified facts in, rows to append and a report out (OMN-20677).

Pure. The effect node reads the ledger and the world; this module decides what a
dangling CLAIM or a stale HOLD is worth and which rows would close it. A decision
that needs a fact it was not given says so (``wanted``) instead of guessing, so
the caller gathers it and asks again.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from omnimarket.models.ledger_reconcile import (
    ModelReconcileDecideRequest,
    ModelReconcileRenderRequest,
    ModelReconcileWanted,
)

from .evidence import (
    COMPLETED,
    ORPHANED,
    UNKNOWN,
    EvidenceContext,
    bind_evidence,
    candidate_clone_names,
    extract_evidence,
    verdict_for,
)
from .facts import FactBook
from .findings import (
    ABANDONED,
    LANE_ACTIVE_HOURS,
    RECONCILER_TICKET,
    Finding,
    build_attention_row,
    build_release_row,
    build_terminal_row,
    claim_scope_text,
    hold_finding,
    hold_reason,
    is_flagged,
    lane_activity,
    open_holds,
    recent_own_rows,
    reconciler_rows,
)
from .ledger_rows import (
    CLAIM_MARK,
    PR_FIELD_RE,
    WATCH_STEP_RE,
    ParseResult,
    ReconcileError,
    Row,
    load_rows,
    positive_control,
    shape_summary,
)
from .pairing import pair_rows


@dataclass
class PlannedAppend:
    index: int
    kind: str  # terminal | attention | release
    finding: Finding
    row: str


@dataclass
class Pipeline:
    findings: list[Finding] = field(default_factory=list)
    parsed: ParseResult = field(default_factory=ParseResult)
    planned: list[PlannedAppend] = field(default_factory=list)
    refused_by_cap: bool = False
    wanted: ModelReconcileWanted = field(default_factory=ModelReconcileWanted)


def validate(request: ModelReconcileDecideRequest) -> None:
    params = request.params
    if (
        params.stale_hours < 0
        or params.since_days < 0
        or (params.silent_hours is not None and params.silent_hours <= 0)
    ):
        raise ReconcileError(
            "time bounds must be nonnegative; --silent-hours must be positive"
        )
    if params.silent_hours is not None and not params.live_roster_known:
        raise ReconcileError(
            "--silent-hours requires --live-lanes with the caller's current live roster"
        )


def run_pipeline(request: ModelReconcileDecideRequest) -> Pipeline:
    validate(request)
    params = request.params
    sources = request.sources
    now = (params.now or sources.read_at).astimezone(UTC).replace(microsecond=0)
    live = (sources.live.name, sources.live.text)
    archives = sorted((s.name, s.text) for s in sources.archives)
    parsed = load_rows(live, archives)
    control_failures = positive_control(parsed)
    if control_failures:
        raise ReconcileError(
            "POSITIVE CONTROL FAILED — the parser saw none of the CLAIM/HOLD rows these "
            "ledger files carry, so any 'clean' verdict would be false: "
            + "; ".join(control_failures)
            + f". Parsed by shape: {shape_summary(parsed)}"
        )
    open_claims = pair_rows(parsed.rows)
    horizon = (
        now - timedelta(days=params.since_days)
        if params.since_days
        else datetime.min.replace(tzinfo=UTC)
    )
    threshold = now - timedelta(hours=params.stale_hours)
    context = EvidenceContext(
        clones=sources.clones,
        aliases=sources.overlay.repo_aliases,
        branch_prefixes=sources.overlay.branch_prefixes,
        registry_name=sources.registry_name,
    )
    live_lanes = frozenset(sources.live_lanes)
    book = FactBook(request.facts)
    ledger_texts = [sources.live.text, *(text for _, text in archives)]
    flagged_rows = reconciler_rows(ledger_texts)
    parsed.window_claims = sum(
        1
        for row in parsed.rows
        if row.kind == "CLAIM" and horizon <= row.ts <= threshold
    )
    active_since = now - timedelta(hours=LANE_ACTIVE_HOURS)
    own_rows = recent_own_rows(ledger_texts[0], active_since)
    lane_latest: dict[str, datetime] = {}
    for text in ledger_texts:
        for lane, when in recent_own_rows(text, horizon).items():
            if lane not in lane_latest or when > lane_latest[lane]:
                lane_latest[lane] = when
    pending_holds = open_holds(parsed.rows)
    hold_lanes = {row.lane for row in pending_holds}
    findings: list[Finding] = []
    for claim in open_claims:
        if claim.ts < horizon or claim.ts > threshold:
            continue
        findings.append(
            _claim_finding(
                claim,
                now,
                context,
                book,
                params.silent_hours,
                live_lanes,
                own_rows,
                lane_latest,
                hold_lanes,
                active_since,
                flagged_rows,
            )
        )
    for hold in pending_holds:
        if horizon <= hold.ts <= threshold:
            findings.append(
                hold_finding(hold, context, book, now, live_lanes, own_rows)
            )
    pipeline = Pipeline(findings=findings, parsed=parsed, wanted=book.wanted())
    if params.apply:
        planned = [
            f
            for f in findings
            if not f.already_flagged
            and not f.held
            and (
                f.verdict in {COMPLETED, ABANDONED}
                or (
                    f.claim.kind == "CLAIM"
                    and f.verdict == ORPHANED
                    and not f.lane_active
                )
            )
        ]
        if params.max_appends is not None and len(planned) > params.max_appends:
            # Refuse the whole pass rather than write a truncated one: a partial
            # pass is indistinguishable afterwards from a complete one.
            for finding in planned:
                finding.applied = "refused-by-cap"
            pipeline.refused_by_cap = True
            return pipeline
        for finding in planned:
            if finding.claim.kind == "HOLD":
                kind, row = "release", build_release_row(finding, now)
            elif finding.verdict in {COMPLETED, ABANDONED}:
                kind, row = "terminal", build_terminal_row(finding, now)
            else:
                kind, row = "attention", build_attention_row(finding, now)
            pipeline.planned.append(
                PlannedAppend(len(pipeline.planned), kind, finding, row)
            )
    return pipeline


def settle(request: ModelReconcileRenderRequest) -> Pipeline:
    """The decision of ``request.decide`` with how each planned append went written onto its finding."""
    pipeline = run_pipeline(request.decide)
    errors = {outcome.index: outcome.error for outcome in request.outcomes}
    applied = {"terminal": "terminal-appended", "attention": "attention-appended"}
    for planned in pipeline.planned:
        error = errors.get(planned.index, "")
        planned.finding.applied = (
            f"append-failed: {error}"
            if error
            else applied.get(planned.kind, "release-appended")
        )
    return pipeline


def _claim_finding(
    claim: Row,
    now: datetime,
    context: EvidenceContext,
    book: FactBook,
    silent_hours: float | None,
    live_lanes: frozenset[str],
    own_rows: dict[str, datetime],
    lane_latest: dict[str, datetime],
    hold_lanes: set[str | None],
    active_since: datetime,
    flagged_rows: list[str],
) -> Finding:
    age_hours = (now - claim.ts).total_seconds() / 3600.0
    evidence = extract_evidence(
        claim.body, context.clones, context.aliases, context.branch_prefixes
    )
    for pr in evidence.prs:
        book.apply_pr(pr)
    candidates = candidate_clone_names(
        claim.body, evidence, context.clones, context.registry_name
    )
    for sha in evidence.shas:
        book.apply_sha(sha, candidates)
    bind_evidence(claim, evidence)
    verdict, detail = verdict_for(evidence)
    finding = Finding(claim, age_hours, verdict, detail, evidence)
    finding.already_flagged = is_flagged(claim, flagged_rows)
    if verdict == ORPHANED:
        finding.lane_active = lane_activity(finding, own_rows, active_since, book)
    finding.held = hold_reason(finding, live_lanes, lane_latest)
    if (
        silent_hours is not None
        and verdict == UNKNOWN
        and not evidence.prs
        and not evidence.shas
        and not evidence.branches
        and not PR_FIELD_RE.search(claim.body)
        and claim.lane
        and claim.lane not in hold_lanes
        and not finding.held
        and not WATCH_STEP_RE.search(claim_scope_text(claim))
        and (now - lane_latest.get(claim.lane, claim.ts)).total_seconds()
        >= silent_hours * 3600
    ):
        finding.verdict = ABANDONED
        finding.detail = (
            "lane absent from the supplied live roster and "
            f"silent for at least {silent_hours:g}h; "
            "no evidence handles or pending holds; work completion is unverified"
        )
    return finding


def render_report(findings: list[Finding], parsed: ParseResult, apply: bool) -> str:
    """The historical report text, line for line."""
    lines: list[str] = []
    emit = lines.append

    emit(
        f"ledger_reconcile ({RECONCILER_TICKET}) — {'APPLY' if apply else 'REPORT'} mode"
    )
    emit(f"rows parsed: {len(parsed.rows)}  unparseable: {len(parsed.unparseable)}")
    emit(f"parsed by shape: {shape_summary(parsed)}")
    emit(
        f"holds parsed: {sum(parsed.holds_parsed.values())} from "
        f"{sum(parsed.hold_marks.values())} HOLD marker lines"
    )
    marks = sum(parsed.claim_marks.values())
    claims = sum(parsed.claims_parsed.values())
    emit(
        f"positive control: {claims} CLAIM rows parsed from {len(parsed.claim_marks)} "
        f"file(s) holding {marks} '{CLAIM_MARK}' line(s); "
        f"{parsed.window_claims} CLAIM rows inside the window"
    )
    if parsed.unparseable:
        emit("\nUNPARSEABLE (skipped, review by hand):")
        for reason in parsed.unparseable:
            emit(f"  - {reason}")
    if not findings:
        emit(
            f"\nNo dangling CLAIM rows inside the window: all {parsed.window_claims} "
            "CLAIM rows there are closed; no stale HOLDs. Ledger is clean."
        )
        return "\n".join(lines) + "\n"
    emit(f"\nDANGLING CLAIMS: {sum(f.claim.kind == 'CLAIM' for f in findings)}")
    emit(f"STALE HOLDS: {sum(f.claim.kind == 'HOLD' for f in findings)}")
    header = f"{'claim ts':<21} {'age(h)':>7} {'verdict':<10} lane / detail"
    emit(header)
    emit("-" * len(header))
    for f in findings:
        lane = f.claim.lane or ",".join(sorted(f.claim.tickets)) or "?"
        flag = " [already-flagged]" if f.already_flagged else ""
        applied = f" [{f.applied}]" if f.applied else ""
        held = f" [HELD, nothing appended: {f.held}]" if f.held else ""
        active = (
            f" [lane active <{LANE_ACTIVE_HOURS:g}h: {f.lane_active}; no attention row]"
            if f.lane_active
            else ""
        )
        emit(
            f"{f.claim.ts.strftime('%Y-%m-%dT%H:%M:%SZ'):<21} {f.age_hours:>7.1f} "
            f"{f.verdict:<10} {f.claim.kind} {lane}{flag}{applied}{held}{active}"
        )
        emit(f"{'':<30} {f.detail[:220]}")
        emit(f"{'':<30} source: {f.claim.source}")
    return "\n".join(lines) + "\n"
