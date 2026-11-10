# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The beta board's state rules, pure: source reads and a clock in, the board out (OMN-23315).

A source is in one of four states from its newest dated verdict: ``pass``, ``fail``, ``stale`` (the newest verdict is older than
the source's bound) and ``unread`` (the source could not be read, has no verdict, or its newest verdict is unknown). A reading that
is missing is never a pass. A criterion is ``fail`` when any of its sources fails, else ``unread`` when any is unread, else
``stale`` when any is stale, else ``pass``; a criterion declared with no source is ``no_reading``. "Since when" is the start of the
unbroken run of equal verdicts that ends at the newest one; when that run reaches the oldest verdict the source gave, the start is
only a lower bound and the board says so.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime

from omnimarket.nodes.node_beta_board_effect.models.model_beta_board import (
    CriterionState,
    ModelBetaBoard,
    ModelBetaBoardOverlay,
    ModelCloudProbe,
    ModelCriterionState,
    ModelSourceRead,
    ModelSourceState,
    SourceState,
)

_SEVERITY: dict[str, int] = {"fail": 0, "unread": 1, "stale": 2, "pass": 3}


def source_state(
    read: ModelSourceRead | None,
    *,
    source_id: str,
    kind: str,
    max_age_seconds: int,
    now: datetime,
    repository: str | None = None,
    workflow: str | None = None,
) -> ModelSourceState:
    """One source's state from its read; a source nobody read, or one that could not be read, is ``unread``."""
    common = {
        "source_id": source_id,
        "kind": kind,
        "repository": repository,
        "workflow": workflow,
    }
    if read is None:
        return ModelSourceState(
            **common, state="unread", detail="no read of this source"
        )
    if read.read == "unread":
        return ModelSourceState(
            **common,
            state="unread",
            detail=read.reason or "the source was not readable",
        )
    if not read.points:
        return ModelSourceState(
            **common,
            state="unread",
            detail=read.reason or "the source holds no reading",
        )
    newest = read.points[0]
    age = max(0, int((now - newest.at).total_seconds()))
    streak_start = newest.at
    reaches_oldest = True
    for point in read.points:
        if point.verdict != newest.verdict:
            reaches_oldest = False
            break
        streak_start = point.at
    if newest.verdict == "unknown":
        state: SourceState = "unread"
        detail = "the newest reading says unknown"
    elif age > max_age_seconds:
        state = "stale"
        detail = (
            f"the newest reading is {age} s old, past its {max_age_seconds} s bound"
        )
    else:
        state = "pass" if newest.verdict == "pass" else "fail"
        detail = ""
    return ModelSourceState(
        **common,
        state=state,
        observed_at=newest.at,
        age_seconds=age,
        since=streak_start,
        since_is_lower_bound=reaches_oldest,
        detail=detail,
        ref=newest.ref,
    )


def criterion_state(
    sources: tuple[ModelSourceState, ...],
) -> tuple[CriterionState, str, datetime | None, bool, int | None]:
    """The criterion's (state, reason, since, since is a lower bound, reading age) from its sources."""
    if not sources:
        return (
            "no_reading",
            "the overlay declares no reading for this criterion",
            None,
            False,
            None,
        )
    state = min(sources, key=lambda s: _SEVERITY[s.state]).state
    ages = [s.age_seconds for s in sources if s.age_seconds is not None]
    age = max(ages) if ages else None
    deciding = [s for s in sources if s.state == state]
    if state == "unread":
        reason = "unread: " + ", ".join(
            f"{s.source_id} ({s.detail})" if s.detail else s.source_id for s in deciding
        )
        return "unread", reason, None, False, age
    if state == "stale":
        stale = [(s.observed_at, s) for s in deciding if s.observed_at is not None]
        stale.sort(key=lambda pair: pair[0])
        reason = "stale: " + ", ".join(s.source_id for s in deciding)
        if not stale:
            return "stale", reason, None, False, age
        return "stale", reason, stale[0][0], False, age
    dated = [(s.since, s) for s in deciding if s.since is not None]
    dated.sort(key=lambda pair: pair[0])
    if state == "fail":
        # The criterion has been failing since its earliest failing source began to fail.
        reason = "failing: " + ", ".join(s.source_id for s in deciding)
        pick = dated[0] if dated else None
    else:
        # The criterion has passed since the last of its sources began to pass.
        reason = f"all {len(sources)} reading(s) pass"
        pick = dated[-1] if dated else None
    if pick is None:
        return state, reason, None, False, age
    return state, reason, pick[0], pick[1].since_is_lower_bound, age


def build_board(
    overlay: ModelBetaBoardOverlay,
    reads: Mapping[str, ModelSourceRead],
    now: datetime,
) -> ModelBetaBoard:
    """The board for every criterion the overlay declares, from the reads keyed by source id."""
    now = now.astimezone(UTC)
    probe_by_id = {p.id: p for p in overlay.probes}
    ledger_by_id = {r.id: r for r in overlay.ledger_readings}
    states: list[ModelCriterionState] = []
    probe_states: dict[str, ModelSourceState] = {}
    probe_criteria: dict[str, list[str]] = {}
    for criterion in overlay.criteria:
        sources: list[ModelSourceState] = []
        for probe_id in criterion.probes:
            probe = probe_by_id[probe_id]
            if probe_id not in probe_states:
                probe_states[probe_id] = source_state(
                    reads.get(probe_id),
                    source_id=probe_id,
                    kind="workflow_run",
                    max_age_seconds=probe.max_age_seconds,
                    now=now,
                    repository=probe.repository,
                    workflow=probe.workflow,
                )
            probe_criteria.setdefault(probe_id, []).append(criterion.id)
            sources.append(probe_states[probe_id])
        for ledger_id in criterion.ledger_readings:
            reading = ledger_by_id[ledger_id]
            sources.append(
                source_state(
                    reads.get(ledger_id),
                    source_id=ledger_id,
                    kind="ledger_row",
                    max_age_seconds=reading.max_age_seconds,
                    now=now,
                )
            )
        state, reason, since, bound, age = criterion_state(tuple(sources))
        states.append(
            ModelCriterionState(
                id=criterion.id,
                condition=criterion.condition,
                proof=criterion.proof,
                owner_ticket=criterion.owner_ticket,
                carriers=tuple(c.ticket for c in criterion.carriers),
                state=state,
                since=since,
                since_is_lower_bound=bound,
                reading_age_seconds=age,
                reason=reason,
                sources=tuple(sources),
            )
        )
    cloud = frozenset(overlay.cloud_repositories)
    cloud_probes = tuple(
        ModelCloudProbe(
            probe_id=p.id,
            repository=p.repository,
            workflow=p.workflow,
            criteria=tuple(probe_criteria.get(p.id, ())),
            state=probe_states[p.id].state if p.id in probe_states else "unread",
        )
        for p in overlay.probes
        if p.repository in cloud
    )
    counts: dict[str, int] = dict.fromkeys(
        ("pass", "fail", "stale", "unread", "no_reading"), 0
    )
    for s in states:
        counts[s.state] += 1
    return ModelBetaBoard(
        generated_at=now,
        milestone_id=overlay.milestone_id,
        milestone_title=overlay.milestone_title,
        met=bool(states) and all(s.state == "pass" for s in states),
        counts=counts,
        criteria=tuple(states),
        cloud_probes=cloud_probes,
        cloud_repositories=overlay.cloud_repositories,
    )
