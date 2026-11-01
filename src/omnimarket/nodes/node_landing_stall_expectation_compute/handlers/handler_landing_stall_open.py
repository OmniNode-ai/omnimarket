# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Open a stall from snapshot rows (OMN-21880).

A stall opens when one required context is red on at least the overlay's share of at
least the overlay's number of open pull requests of one repository. It is judged per
repository and context, never per pull request or per snapshot: one stall holds its
first instant while the red holds, however many snapshots read it. At the overlay's
exception time it is due one exception row and one cause lane, and the landing
controller is to send no further repair workers at that context; at the question time,
with no eligible repair, one plain question. It closes after the overlay's number of
consecutive clear snapshots. Rollup contexts the overlay names are not judged: they
are red wherever anything is.

This is the pure rule only. Nothing here dispatches, merges or writes a row: the
controller wiring is a later slice and is checked against ``formal/landing_stall``.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from omnimarket.nodes.node_landing_stall_expectation_compute.models.model_landing_stall_open import (
    EnumStallPhase,
    ModelContextStall,
    ModelLandingStallOpenRequest,
    ModelLandingStallOpenResult,
    ModelLandingStallThresholds,
    ModelRepoContextSnapshot,
)


class _Episode:
    """The running state of one stall while the snapshots are read in order."""

    def __init__(self, snapshot: ModelRepoContextSnapshot, red: int) -> None:
        self.opened_at = snapshot.observed_at
        self.opened_red = red
        self.opened_open = snapshot.open_prs
        self.red = red
        self.open = snapshot.open_prs
        self.closed_at: datetime | None = None
        self.clear_run = 0


def _holds(
    thresholds: ModelLandingStallThresholds,
    snapshot: ModelRepoContextSnapshot,
    red: int,
) -> bool:
    return (
        snapshot.open_prs >= thresholds.min_open_prs
        and red * 100 >= thresholds.share_percent * snapshot.open_prs
    )


def _episodes(
    thresholds: ModelLandingStallThresholds,
    snapshots: list[ModelRepoContextSnapshot],
    context: str,
) -> list[_Episode]:
    episodes: list[_Episode] = []
    running: _Episode | None = None
    for snapshot in snapshots:
        red = snapshot.red_by_context.get(context, 0)
        holds = _holds(thresholds, snapshot, red)
        if running is None:
            if holds:
                running = _Episode(snapshot, red)
                episodes.append(running)
            continue
        running.red, running.open = red, snapshot.open_prs
        if holds:
            running.clear_run = 0
            continue
        running.clear_run += 1
        if running.clear_run >= thresholds.clear_snapshots:
            running.closed_at = snapshot.observed_at
            running = None
    return episodes


class HandlerLandingStallOpen:
    """Pure: the same snapshots give the same stalls."""

    def handle(
        self, request: ModelLandingStallOpenRequest
    ) -> ModelLandingStallOpenResult:
        thresholds = request.thresholds
        aggregates = set(thresholds.aggregate_contexts)
        repairs = {(r.repo, r.context) for r in request.eligible_repairs}
        by_repo: dict[str, list[ModelRepoContextSnapshot]] = {}
        for snapshot in sorted(request.snapshots, key=lambda s: s.observed_at):
            by_repo.setdefault(snapshot.repo, []).append(snapshot)
        stalls: list[ModelContextStall] = []
        for repo in sorted(by_repo):
            snapshots = by_repo[repo]
            evaluated_at = snapshots[-1].observed_at
            contexts = sorted(
                {c for s in snapshots for c in s.red_by_context} - aggregates
            )
            for context in contexts:
                for episode in _episodes(thresholds, snapshots, context):
                    exception_due = episode.opened_at + timedelta(
                        minutes=thresholds.exception_minutes
                    )
                    question_due = episode.opened_at + timedelta(
                        minutes=thresholds.question_minutes
                    )
                    if episode.closed_at is not None:
                        phase = EnumStallPhase.CLOSED
                    elif (
                        evaluated_at >= question_due and (repo, context) not in repairs
                    ):
                        phase = EnumStallPhase.QUESTION_DUE
                    elif evaluated_at >= exception_due:
                        phase = EnumStallPhase.EXCEPTION_DUE
                    else:
                        phase = EnumStallPhase.OPEN
                    stalls.append(
                        ModelContextStall(
                            repo=repo,
                            context=context,
                            phase=phase,
                            opened_at=episode.opened_at,
                            exception_due_at=exception_due,
                            question_due_at=question_due,
                            closed_at=episode.closed_at,
                            opened_red_prs=episode.opened_red,
                            opened_open_prs=episode.opened_open,
                            red_prs=episode.red,
                            open_prs=episode.open,
                            evaluated_at=evaluated_at,
                        )
                    )
        return ModelLandingStallOpenResult(stalls=tuple(stalls))


__all__ = ["HandlerLandingStallOpen"]
