# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerDevMergeRedeployVerify -- read back that the dev lane carries a merge (OMN-22478).

Canonical def-B handler: ``handle(pending, completed) -> ModelDevMergeVerdict``. It runs after
the redeploy orchestrator's terminal event for the merge's correlation id. It replaces the
Actions ``verify-sibling-converged`` job: instead of a job on the verify runner pool, the
node reads the running lane's own build provenance and asks the repository clone whether the
vendored commit is the merge or a descendant of it (containment, not equality: a later merge
may legitimately be in the same build).

A redeploy that did not reach DONE is ``deploy_not_done`` and the lane is not read at all, so a
failed deploy is never reported as a lane that is merely behind.
"""

from __future__ import annotations

from omnimarket.events.runtime_deployment import (
    EnumRedeployPhase,
    ModelRedeployCompletedEvent,
)
from omnimarket.models.dev_merge_redeploy import (
    EnumDevMergeOutcome,
)
from omnimarket.models.dev_merge_redeploy_overlay import (
    ModelDevMergeRedeployPolicy,
)
from omnimarket.nodes.node_dev_merge_redeploy_trigger_effect.handlers.protocol_dev_merge_redeploy import (
    LaneUnreadableError,
    ProtocolLaneRevision,
)
from omnimarket.nodes.node_dev_merge_redeploy_trigger_effect.models.model_dev_merge_decision import (
    ModelDevMergePending,
    ModelDevMergeVerdict,
)

#: The ``final_phase`` of a receipt written in observe mode: no redeploy phase was seen.
OBSERVED_PHASE = "observed"


class HandlerDevMergeRedeployVerify:
    def __init__(
        self, policy: ModelDevMergeRedeployPolicy, *, lane: ProtocolLaneRevision
    ) -> None:
        self._policy = policy
        self._lane = lane

    def handle(
        self, pending: ModelDevMergePending, completed: ModelRedeployCompletedEvent
    ) -> ModelDevMergeVerdict:
        phase = completed.final_phase
        if phase != EnumRedeployPhase.DONE:
            return ModelDevMergeVerdict(
                outcome=EnumDevMergeOutcome.DEPLOY_NOT_DONE,
                final_phase=str(phase),
                message=(completed.error_message or "no error message")[-2000:],
            )
        event = pending.event
        repo = self._policy.for_repo(event.repo)
        if repo is None:
            return ModelDevMergeVerdict(
                outcome=EnumDevMergeOutcome.LANE_UNREADABLE,
                final_phase=str(phase),
                message=f"{event.repo} is no longer in the policy",
            )
        try:
            lane_sha = self._lane.vendored_revision(
                repo.source_repo, repo.provenance_section
            )
            carries = self._lane.lane_carries(
                repo.clone_dir, event.base, event.merge_sha, lane_sha
            )
        except LaneUnreadableError as exc:
            return ModelDevMergeVerdict(
                outcome=EnumDevMergeOutcome.LANE_UNREADABLE,
                final_phase=str(phase),
                message=f"{type(exc).__name__}: {exc}"[-2000:],
            )
        if carries:
            return ModelDevMergeVerdict(
                outcome=EnumDevMergeOutcome.VERIFIED,
                final_phase=str(phase),
                lane_revision=lane_sha,
                message=f"lane vendors {lane_sha[:12]}, which carries {event.merge_sha[:12]}",
            )
        return ModelDevMergeVerdict(
            outcome=EnumDevMergeOutcome.LANE_BEHIND,
            final_phase=str(phase),
            lane_revision=lane_sha,
            message=f"lane vendors {lane_sha[:12]}, which does not carry {event.merge_sha[:12]}",
        )

    def observe(self, pending: ModelDevMergePending) -> ModelDevMergeVerdict:
        """One read of the lane for a merge another trigger is redeploying.

        There is no terminal event to wait for, so the verdict is only the lane's state now:
        ``verified`` once the lane carries the merge, ``lane_behind`` while it does not and
        ``lane_unreadable`` when it cannot be read. The caller reads again until a deadline.
        """
        event = pending.event
        repo = self._policy.for_repo(event.repo)
        if repo is None:
            return ModelDevMergeVerdict(
                outcome=EnumDevMergeOutcome.LANE_UNREADABLE,
                final_phase=OBSERVED_PHASE,
                message=f"{event.repo} is no longer in the policy",
            )
        try:
            lane_sha = self._lane.vendored_revision(
                repo.source_repo, repo.provenance_section
            )
            carries = self._lane.lane_carries(
                repo.clone_dir, event.base, event.merge_sha, lane_sha
            )
        except LaneUnreadableError as exc:
            return ModelDevMergeVerdict(
                outcome=EnumDevMergeOutcome.LANE_UNREADABLE,
                final_phase=OBSERVED_PHASE,
                message=f"{type(exc).__name__}: {exc}"[-2000:],
            )
        outcome = (
            EnumDevMergeOutcome.VERIFIED if carries else EnumDevMergeOutcome.LANE_BEHIND
        )
        verb = "carries" if carries else "does not carry"
        return ModelDevMergeVerdict(
            outcome=outcome,
            final_phase=OBSERVED_PHASE,
            lane_revision=lane_sha,
            message=f"lane vendors {lane_sha[:12]}, which {verb} {event.merge_sha[:12]}",
        )


__all__ = ["OBSERVED_PHASE", "HandlerDevMergeRedeployVerify"]
