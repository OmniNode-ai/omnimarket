# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Which pull requests are the work of a task row, and whether that work has merged (OMN-21930).

A task row is built when the tickets it names have merged pull requests. The pull requests come from both halves of
the watcher record: its open and merged pull requests, and its merge list, which holds far more merged pull requests
than the pull-request map keeps. A pull request belongs to a ticket by the tickets its own title and branch name,
never by a mention in its body, and a pull request that names other tickets is not the work of a row that names
different ones, whatever a plan table listed beside the row.
"""

from __future__ import annotations

import functools
import re
from dataclasses import dataclass

from omnimarket.models.model_factory_floor_input import (
    ModelFactoryFloorRequest,
    ModelFloorManifestTask,
    PrState,
)
from omnimarket.nodes.node_factory_floor_compute.handlers.floor_lanes import pr_key

TICKET_IN_NAME_RE = re.compile(r"\bOMN-\d+\b", re.IGNORECASE)


@dataclass(frozen=True)
class PrRecord:
    """One open or merged pull request and the tickets its title and branch name."""

    key: str
    state: PrState
    title: str
    head_ref: str
    tickets: frozenset[str]


def name_tickets(*names: str) -> frozenset[str]:
    """The tickets a title or a branch name carries (``omn-21635-w0-1`` is OMN-21635)."""
    return frozenset(
        found.upper() for name in names for found in TICKET_IN_NAME_RE.findall(name)
    )


def pr_records(request: ModelFactoryFloorRequest) -> dict[str, PrRecord]:
    """Every open or merged pull request the watcher knows, by key; a closed unmerged one is nobody's work."""
    records: dict[str, PrRecord] = {}
    for pr in request.watcher.prs:
        if pr.state in {"OPEN", "MERGED"}:
            records[pr_key(pr.key)] = PrRecord(
                key=pr.key,
                state=pr.state,
                title=pr.title,
                head_ref=pr.head_ref,
                tickets=name_tickets(pr.title, pr.head_ref),
            )
    for merge in request.watcher.merges:
        records[pr_key(merge.key)] = PrRecord(
            key=merge.key,
            state="MERGED",
            title=merge.title,
            head_ref=merge.head_ref,
            tickets=name_tickets(merge.title, merge.head_ref),
        )
    return records


@functools.lru_cache(maxsize=4096)
def _id_pattern(task_id: str) -> re.Pattern[str]:
    """One compiled pattern per task id: the mapping asks it of every pull request for every task."""
    return re.compile(rf"(?<![\w.]){re.escape(task_id)}(?![\w.])")


def _id_in(task_id: str, text: str) -> bool:
    return _id_pattern(task_id).search(text) is not None


def _is_work_of(
    record: PrRecord,
    task: ModelFloorManifestTask,
    scope: frozenset[str],
    task_tickets: frozenset[str],
) -> bool:
    if record.tickets & task_tickets:
        return True
    if task.tickets:
        # The row names its tickets, and this pull request names others: it is some other row's work.
        return False
    named = _id_in(task.id, record.title) or _id_in(task.id, record.head_ref)
    if not named:
        return False
    # A bare id (T1, S3) names a different task in every plan; a task read from a plan's own paragraphs takes it
    # only from a pull request that names a ticket of this plan.
    return not task.scoped or bool(record.tickets & scope)


def task_prs(
    task: ModelFloorManifestTask,
    scope: tuple[str, ...],
    records: dict[str, PrRecord],
) -> tuple[str, ...]:
    """The pull requests that are the work of the task, by the tickets in their titles and branches.

    A pull request the plan lists for the row stays when the watcher does not know its identity (a plan may name a pull
    request in another repository) or when it is the row's by ticket. A known title or branch with no matching ticket,
    including a dependency update with no ticket at all, is not evidence for a row that names its tickets.
    """
    wanted = frozenset(scope)
    task_tickets = frozenset(task.tickets)
    listed = tuple(
        pr
        for pr in task.prs
        if (record := records.get(pr_key(pr))) is None
        or not task.tickets
        or not (record.title or record.head_ref)
        or bool(record.tickets & task_tickets)
    )
    inferred = tuple(
        record.key
        for record in records.values()
        if _is_work_of(record, task, wanted, task_tickets)
    )
    return tuple(sorted(set(listed + inferred)))


def is_built(
    task: ModelFloorManifestTask,
    prs: tuple[str, ...],
    records: dict[str, PrRecord],
) -> bool:
    """True when the work merged and none of it is still open.

    A row that names tickets is built when every one of them has a merged pull request; a row that names none is built
    when it has pull requests and every one the watcher knows is merged.
    """
    attributed = [records[pr_key(pr)] for pr in prs if pr_key(pr) in records]
    if any(record.state == "OPEN" for record in attributed):
        return False
    merged = [record for record in attributed if record.state == "MERGED"]
    if task.tickets:
        # A pull request the plan lists whose title and branch name no ticket cannot be told from the row's own.
        return all(
            any(ticket in record.tickets or not record.tickets for record in merged)
            for ticket in task.tickets
        )
    return bool(prs) and all(
        pr_key(pr) in records and records[pr_key(pr)].state == "MERGED" for pr in prs
    )
