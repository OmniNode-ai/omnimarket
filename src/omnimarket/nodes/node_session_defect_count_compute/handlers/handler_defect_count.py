# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Per-session defect counters (compaction measurement plan, task CM.5).

Pure: it reads typed slices of the local pull request watcher snapshot, its
check-run history per head and the ledger, and makes no GitHub call, no file
read and no clock read (the caller passes `as_of`).

Defect identity is by cause. A red check is `(pull request, head sha, check
name)`, a revert is the reverting pull request, a retracted claim is the
corrected row's stamp and a CORRECTION is its own stamp. Signals of more than
one kind that describe one pull request's failure collapse to that pull
request. Each defect is owned by exactly one window: windows around a
boundary are cut at the midpoint to a neighbouring boundary, so they never
overlap, and an event belongs to the window that contains its timestamp.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta

from ..models.model_defect_count import (
    DefectKind,
    Finality,
    ModelCheckRun,
    ModelDefect,
    ModelDefectCountRequest,
    ModelDefectCountResult,
    ModelExcludedRed,
    ModelLedgerRow,
    ModelPullRequest,
    ModelRedHead,
    ModelSession,
    ModelSessionDefects,
    ModelUnmergedPr,
    ModelWindowCount,
    RateStatus,
)
from .nondefect_classes import classify_red, is_red


def _stamp(value: datetime) -> str:
    return value.isoformat()


class _Signal:
    __slots__ = ("at", "identity", "kind", "pr_ref")

    def __init__(
        self, identity: str, kind: DefectKind, pr_ref: str | None, at: datetime
    ) -> None:
        self.identity = identity
        self.kind = kind
        self.pr_ref = pr_ref
        self.at = at


class _Window:
    __slots__ = ("boundary", "end", "index", "side", "start")

    def __init__(
        self,
        index: int,
        side: str,
        boundary: datetime,
        start: datetime,
        end: datetime,
    ) -> None:
        self.index = index
        self.side = side
        self.boundary = boundary
        self.start = start
        self.end = end

    @property
    def window_id(self) -> str:
        return f"b{self.index}:{self.side}"

    def contains(self, moment: datetime) -> bool:
        return self.start <= moment < self.end


def build_windows(
    boundaries: tuple[datetime, ...], length: timedelta
) -> tuple[_Window, ...]:
    """Windows of equal nominal length, cut at the midpoint to a neighbour."""
    windows: list[_Window] = []
    for i, b in enumerate(boundaries):
        start = b - length
        end = b + length
        if i > 0:
            start = max(start, boundaries[i - 1] + (b - boundaries[i - 1]) / 2)
        if i + 1 < len(boundaries):
            end = min(end, b + (boundaries[i + 1] - b) / 2)
        windows.append(_Window(i, "before", b, start, b))
        windows.append(_Window(i, "after", b, b, end))
    return tuple(windows)


def _owner(windows: tuple[_Window, ...], moment: datetime) -> _Window | None:
    owners = [w for w in windows if w.contains(moment)]
    if len(owners) > 1:
        raise AssertionError("windows overlap")
    return owners[0] if owners else None


def _in_family(lane: str, family: str) -> bool:
    return lane == family or lane.startswith(f"{family}-")


def _real_reds(
    pr: ModelPullRequest,
) -> tuple[list[_Signal], list[ModelExcludedRed], list[ModelRedHead]]:
    """Signals, exclusions and red heads for the required reds of one pull request."""
    signals: list[_Signal] = []
    excluded: list[ModelExcludedRed] = []
    red_heads: list[ModelRedHead] = []
    required = set(pr.required_checks)
    for head in pr.heads:
        kept: dict[str, list[ModelCheckRun]] = defaultdict(list)
        for check in head.checks:
            if not is_red(check) or check.name not in required:
                continue
            cls = classify_red(check, head, pr)
            if cls is None:
                kept[check.name].append(check)
            elif not any(
                e.head_sha == head.sha and e.check == check.name for e in excluded
            ):
                excluded.append(
                    ModelExcludedRed(
                        pr_ref=pr.ref,
                        head_sha=head.sha,
                        check=check.name,
                        nondefect_class=cls,
                    )
                )
        for name, copies in kept.items():
            times = [c.completed_at for c in copies if c.completed_at is not None]
            when = min(times) if times else pr.opened_at
            signals.append(
                _Signal(f"red:{pr.ref}@{head.sha}/{name}", "red_check", pr.ref, when)
            )
        if kept:
            red_heads.append(
                ModelRedHead(head_sha=head.sha, checks=tuple(sorted(kept)))
            )
    return signals, excluded, red_heads


def _ledger_signals(
    session: ModelSession, rows: tuple[ModelLedgerRow, ...], follow_up: timedelta
) -> list[_Signal]:
    by_stamp = {r.stamp: r for r in rows}
    horizon = session.ends_at + follow_up
    signals: list[_Signal] = []
    for r in rows:
        if r.row_type != "CORRECTION":
            continue
        if _in_family(r.lane, session.lane_family) and (
            session.starts_at <= r.stamp <= horizon
        ):
            signals.append(
                _Signal(
                    f"correction:{_stamp(r.stamp)}", "correction", r.pr_ref, r.stamp
                )
            )
        target = by_stamp.get(r.corrects) if r.corrects is not None else None
        if (
            target is not None
            and _in_family(target.lane, session.lane_family)
            and session.starts_at <= target.stamp <= session.ends_at
        ):
            signals.append(
                _Signal(
                    f"retraction:{_stamp(target.stamp)}",
                    "retracted_claim",
                    target.pr_ref or r.pr_ref,
                    target.stamp,
                )
            )
    return signals


def _collapse(signals: list[_Signal]) -> list[tuple[_Signal, tuple[str, ...]]]:
    """One defect per cause; several kinds on one pull request become that pull request."""
    unique: dict[str, _Signal] = {}
    for s in signals:
        unique.setdefault(s.identity, s)
    by_pr: dict[str, list[_Signal]] = defaultdict(list)
    loose: list[_Signal] = []
    for s in unique.values():
        (by_pr[s.pr_ref] if s.pr_ref else loose).append(s)
    out: list[tuple[_Signal, tuple[str, ...]]] = [(s, (s.identity,)) for s in loose]
    for ref, group in by_pr.items():
        if len({s.kind for s in group}) > 1:
            out.append(
                (
                    _Signal(f"pr:{ref}", "pull_request", ref, min(s.at for s in group)),
                    tuple(sorted(s.identity for s in group)),
                )
            )
        else:
            out.extend((s, (s.identity,)) for s in group)
    return sorted(out, key=lambda pair: (pair[0].at, pair[0].identity))


def _rate(defects: int, merged: int) -> tuple[float | None, RateStatus]:
    if merged == 0:
        return None, "no denominator"
    return defects / merged, "rate"


def _finality(as_of: datetime, last: datetime, follow_up: timedelta) -> Finality:
    return "FINAL" if as_of >= last + follow_up else "PROVISIONAL"


class HandlerSessionDefectCount:
    """Count defects per session, per merged pull request, in equal windows."""

    def handle(self, request: ModelDefectCountRequest) -> ModelDefectCountResult:
        follow_up = timedelta(days=request.follow_up_days)
        length = timedelta(seconds=request.window_seconds)
        return ModelDefectCountResult(
            as_of=request.as_of,
            sessions=tuple(
                self._session(request, s, length, follow_up) for s in request.sessions
            ),
        )

    def _session(
        self,
        request: ModelDefectCountRequest,
        session: ModelSession,
        length: timedelta,
        follow_up: timedelta,
    ) -> ModelSessionDefects:
        prs = [p for p in request.pull_requests if p.session_id == session.session_id]
        merged = [p for p in prs if p.state == "merged"]
        merged_by_ref = {p.ref: p for p in merged}

        signals: list[_Signal] = []
        excluded: list[ModelExcludedRed] = []
        for p in merged:
            found, skipped, _ = _real_reds(p)
            signals += found
            excluded += skipped
        for rv in request.reverts:
            target = merged_by_ref.get(rv.reverted_pr)
            if (
                target is not None
                and target.merged_at is not None
                and timedelta(0) <= rv.merged_at - target.merged_at <= follow_up
            ):
                signals.append(
                    _Signal(
                        f"revert:{rv.reverting_pr}",
                        "revert",
                        rv.reverted_pr,
                        rv.merged_at,
                    )
                )
        signals += _ledger_signals(session, request.ledger_rows, follow_up)

        unmerged: list[ModelUnmergedPr] = []
        for p in prs:
            if p.state == "closed_unmerged":
                _, _, heads = _real_reds(p)
                unmerged.append(ModelUnmergedPr(ref=p.ref, red_heads=tuple(heads)))

        windows = build_windows(session.boundaries, length)
        defects: list[ModelDefect] = []
        per_window: dict[str, int] = defaultdict(int)
        for signal, parts in _collapse(signals):
            owner = _owner(windows, signal.at)
            if owner is not None:
                per_window[owner.window_id] += 1
            defects.append(
                ModelDefect(
                    identity=signal.identity,
                    kind=signal.kind,
                    pr_ref=signal.pr_ref,
                    at=signal.at,
                    signals=parts,
                    window_id=owner.window_id if owner else None,
                )
            )

        merged_per_window: dict[str, list[datetime]] = defaultdict(list)
        for p in merged:
            assert p.merged_at is not None
            owner = _owner(windows, p.merged_at)
            if owner is not None:
                merged_per_window[owner.window_id].append(p.merged_at)

        window_counts = []
        for w in windows:
            n = len(merged_per_window[w.window_id])
            rate, status = _rate(per_window[w.window_id], n)
            last = max([w.end, *merged_per_window[w.window_id]])
            window_counts.append(
                ModelWindowCount(
                    window_id=w.window_id,
                    boundary=w.boundary,
                    side="before" if w.side == "before" else "after",
                    start=w.start,
                    end=w.end,
                    defects=per_window[w.window_id],
                    merged_prs=n,
                    rate=rate,
                    rate_status=status,
                    finality=_finality(request.as_of, last, follow_up),
                )
            )

        rate, status = _rate(len(defects), len(merged))
        last_session = max(
            [session.ends_at, *(p.merged_at for p in merged if p.merged_at)]
        )
        return ModelSessionDefects(
            session_id=session.session_id,
            merged_prs=len(merged),
            defect_count=len(defects),
            rate=rate,
            rate_status=status,
            finality=_finality(request.as_of, last_session, follow_up),
            defects=tuple(defects),
            windows=tuple(window_counts),
            outside_window_defects=sum(1 for d in defects if d.window_id is None),
            excluded_reds=tuple(excluded),
            unmerged_prs=tuple(unmerged),
        )
