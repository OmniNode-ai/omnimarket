# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One read of a base branch's merge queue (OMN-17427)."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class ModelMergeQueueRead(BaseModel):
    """The merge-group commits queued on one base branch at read time.

    complete is False when the read failed, the branch has no queue, or the
    entries list was cut short: a run is never judged against an incomplete read.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    base_branch: str
    complete: bool
    group_shas: tuple[str, ...] = ()

    @classmethod
    def from_github(
        cls, base_branch: str, body: dict[str, object] | None
    ) -> ModelMergeQueueRead:
        """Parse the merge-queue entries query; anything unexpected is an incomplete read."""
        unread = cls(base_branch=base_branch, complete=False)
        data = (body or {}).get("data")
        repository = data.get("repository") if isinstance(data, dict) else None
        queue = repository.get("mergeQueue") if isinstance(repository, dict) else None
        entries = queue.get("entries") if isinstance(queue, dict) else None
        if not isinstance(entries, dict):
            return unread
        nodes = entries.get("nodes")
        total = entries.get("totalCount")
        if not isinstance(nodes, list) or not isinstance(total, int):
            return unread
        if total > len(nodes):
            return unread
        shas: list[str] = []
        for node in nodes:
            commit = node.get("headCommit") if isinstance(node, dict) else None
            oid = commit.get("oid") if isinstance(commit, dict) else None
            if isinstance(oid, str) and oid:
                shas.append(oid)
        return cls(base_branch=base_branch, complete=True, group_shas=tuple(shas))
