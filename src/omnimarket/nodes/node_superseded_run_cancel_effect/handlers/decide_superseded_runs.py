# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The supersede rule, a pure function of the run list and the queue read (OMN-17427).

Nothing else is an input: no watcher snapshot, no cache, no clock. A sweep that
judged heads against a watcher snapshot older than a push cancelled four runs on
the new head; here the new head's own runs are in the run list, so they set the
PR's current head and are never selected.

- **pull_request run.** The PR's current head is the head of its newest
  pull_request run in the list (by created_at, run id breaking a tie). A run is
  superseded when its head differs from that, for every PR it belongs to. A PR
  whose newest runs already finished has no current head in the list, so its
  older runs are left alone: the rule errs toward keeping a run.
- **merge_group run.** The run's head is the merge-group commit. It is
  superseded when the queue read of its base branch is complete and holds no
  entry with that commit. A base with no complete queue read is not judged.
- **Any other event, or a run tied to no PR,** is never selected.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

from omnimarket.nodes.node_superseded_run_cancel_effect.models import (
    EnumSupersededReason,
    ModelMergeQueueRead,
    ModelSupersededRun,
    ModelWorkflowRunFact,
)

PULL_REQUEST_EVENT = "pull_request"
MERGE_GROUP_EVENT = "merge_group"
_MERGE_GROUP_BRANCH = re.compile(r"^gh-readonly-queue/(?P<base>.+)/pr-\d+-[0-9a-f]+$")


def merge_group_base(run: ModelWorkflowRunFact) -> str | None:
    """The base branch a merge_group run builds for, read from its queue ref name."""
    if run.event != MERGE_GROUP_EVENT:
        return None
    match = _MERGE_GROUP_BRANCH.match(run.head_branch)
    return match.group("base") if match else None


def merge_group_bases(runs: Iterable[ModelWorkflowRunFact]) -> tuple[str, ...]:
    """The distinct base branches whose queue the merge_group runs need read."""
    return tuple(sorted({b for r in runs if (b := merge_group_base(r)) is not None}))


def _order(run: ModelWorkflowRunFact) -> tuple[str, int]:
    return (run.created_at, run.run_id)


def current_heads(runs: Iterable[ModelWorkflowRunFact]) -> dict[int, str]:
    """PR number -> head of that PR's newest pull_request run in the list."""
    newest: dict[int, ModelWorkflowRunFact] = {}
    for run in runs:
        if run.event != PULL_REQUEST_EVENT:
            continue
        for pr in run.pr_numbers:
            held = newest.get(pr)
            if held is None or _order(run) > _order(held):
                newest[pr] = run
    return {pr: run.head_sha for pr, run in newest.items()}


def decide_superseded_runs(
    runs: Iterable[ModelWorkflowRunFact],
    queues: Iterable[ModelMergeQueueRead],
) -> tuple[ModelSupersededRun, ...]:
    """The runs to cancel, ordered by run id."""
    listed = tuple(runs)
    heads = current_heads(listed)
    queue_by_base = {q.base_branch: q for q in queues if q.complete}
    selected: list[ModelSupersededRun] = []
    for run in listed:
        if run.event == PULL_REQUEST_EVENT and run.pr_numbers:
            stale = [pr for pr in run.pr_numbers if heads[pr] != run.head_sha]
            if len(stale) == len(run.pr_numbers):
                selected.append(
                    ModelSupersededRun(
                        run_id=run.run_id,
                        workflow_name=run.workflow_name,
                        event=run.event,
                        head_sha=run.head_sha,
                        reason=EnumSupersededReason.PR_HEAD_MOVED,
                        pr_number=stale[0],
                        current_head_sha=heads[stale[0]],
                    )
                )
        elif run.event == MERGE_GROUP_EVENT:
            base = merge_group_base(run)
            queue = queue_by_base.get(base) if base is not None else None
            if queue is not None and run.head_sha not in queue.group_shas:
                selected.append(
                    ModelSupersededRun(
                        run_id=run.run_id,
                        workflow_name=run.workflow_name,
                        event=run.event,
                        head_sha=run.head_sha,
                        reason=EnumSupersededReason.MERGE_GROUP_LEFT_QUEUE,
                        base_branch=base,
                    )
                )
    return tuple(sorted(selected, key=lambda s: s.run_id))


def unjudged_merge_group_runs(
    runs: Iterable[ModelWorkflowRunFact],
    queues: Iterable[ModelMergeQueueRead],
) -> int:
    """How many merge_group runs had no complete queue read to be judged against."""
    complete = {q.base_branch for q in queues if q.complete}
    return sum(
        1
        for r in runs
        if r.event == MERGE_GROUP_EVENT and merge_group_base(r) not in complete
    )
