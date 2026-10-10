# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The next stale-summary action for a head (pure, definition-B)."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from omnimarket.nodes.node_pr_lifecycle_triage_compute.models.enum_stale_step import (
    EnumStaleStep,
)
from omnimarket.nodes.node_pr_lifecycle_triage_compute.models.model_stale_step_facts import (
    ModelStaleStepFacts,
)
from omnimarket.nodes.node_pr_lifecycle_triage_compute.models.model_stale_step_verdict import (
    ModelStaleStepVerdict,
)

MAX_STALE_REFRESHES = (
    2  # update-branches per PR across heads, so a head that re-stales cannot loop
)


def stale_step(kind: str, mem: Mapping[str, Any] | None, head: str) -> str | None:
    """The next stale-summary action for ``head``: ``rerun`` (once per head), then ``update_branch`` (once per
    head, ``MAX_STALE_REFRESHES`` per PR), then None (the decision decides). ``refresh_first`` (a provider incident)
    and ``refresh`` (an unexpanded skip) reverse the two: the update-branch, then the one rerun, which a head the
    update-branch left unchanged needs (omnibase_infra#4745 head 6117d264, OMN-20413)."""
    m = mem or {}
    done = list((m.get("heads") or {}).get(head) or ())
    if kind in ("refresh_first", "refresh"):
        if (
            "update_branch" not in done
            and int(m.get("refreshes") or 0) < MAX_STALE_REFRESHES
        ):
            return "update_branch"
        return "rerun" if "rerun" not in done else None
    if kind == "rerun" and "rerun" not in done:
        return "rerun"
    if (
        "update_branch" not in done
        and int(m.get("refreshes") or 0) < MAX_STALE_REFRESHES
    ):
        return "update_branch"
    return None


class HandlerNextStaleStep:
    """The stale-summary remedy to take next on a head (pure, definition-B)."""

    def handle(self, request: ModelStaleStepFacts) -> ModelStaleStepVerdict:
        mem = (
            None if request.mem is None else request.mem.model_dump(exclude_unset=True)
        )
        step = stale_step(request.kind.value, mem, request.head)
        return ModelStaleStepVerdict(step=None if step is None else EnumStaleStep(step))


__all__: list[str] = ["MAX_STALE_REFRESHES", "HandlerNextStaleStep", "stale_step"]
