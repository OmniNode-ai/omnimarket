# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Act-mode writer of the landing tick orchestrator onto node_pr_landing_github_effect (OMN-23393, OMN-23405).

The tick orchestrator calls a writer only in an enabled act mode. This writer turns each
decision action into one command of the single GitHub landing effect, or a typed refusal, and
publishes the command on the effect's own command topic. It never calls GitHub itself.

- merge, pinned to its head, is ``enqueue`` where the base lands through a merge queue and
  ``arm_auto_merge`` otherwise; both carry the head as GitHub's expected head, so a moved head
  is refused by GitHub as well as here.
- rerun and eligibility_rerun are ``rerun_runs`` of the head's failed runs.
- update_branch is ``update_branch`` at the head.
- every other action (workers, companions, escalations, observe-only) is not a GitHub write
  of this effect and is refused as such; its own node performs it.
- an action outside the overlay's cutover repository is refused: the script owns it.

The PR's node id, merge-queue base and failed run ids are read from the tick's own facts
(ModelLandingPrFacts), never from a side channel: a write the facts cannot pin is refused.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from pathlib import Path
from uuid import UUID, uuid4

import yaml

from omnimarket.events.pr_landing_github.enum_pr_landing_github_mode import (
    EnumPrLandingGithubMode,
)
from omnimarket.events.pr_landing_github.enum_pr_landing_github_operation import (
    EnumPrLandingGithubOperation,
)
from omnimarket.events.pr_landing_github.model_pr_landing_github_request import (
    ModelPrLandingGithubRequest,
)
from omnimarket.nodes.node_pr_landing_decision_compute.models.enum_landing import (
    EnumLandingActionKind,
)
from omnimarket.nodes.node_pr_landing_decision_compute.models.model_landing_decision import (
    ModelLandingAction,
)
from omnimarket.nodes.node_pr_landing_decision_compute.models.model_landing_facts import (
    ModelLandingFacts,
    ModelLandingPrFacts,
)
from omnimarket.nodes.node_pr_landing_tick_orchestrator.handlers.tick_ports import (
    ProtocolTickPublisher,
)
from omnimarket.nodes.node_pr_landing_tick_orchestrator.models.model_tick_write import (
    EnumTickWriteRefusal,
    ModelTickWriteOutcome,
)

EFFECT_CONTRACT = (
    Path(__file__).resolve().parents[2]
    / "node_pr_landing_github_effect"
    / "contract.yaml"
)
_RERUNS = frozenset(
    {EnumLandingActionKind.RERUN, EnumLandingActionKind.ELIGIBILITY_RERUN}
)


def effect_command_topic() -> str:
    """The GitHub landing effect's command topic, as its own contract declares it."""
    contract = yaml.safe_load(EFFECT_CONTRACT.read_text(encoding="utf-8"))
    topics = contract["event_bus"]["subscribe_topics"]
    return str(topics[0])


def _refused(
    action: ModelLandingAction, refusal: EnumTickWriteRefusal, detail: str
) -> ModelTickWriteOutcome:
    return ModelTickWriteOutcome(action=action, refusal=refusal, detail=detail)


def plan_write(
    action: ModelLandingAction,
    *,
    cutover_repository: str,
    pr_facts: Mapping[str, ModelLandingPrFacts],
    mode: EnumPrLandingGithubMode,
    correlation_id: UUID,
) -> ModelTickWriteOutcome:
    """One action's GitHub landing command, or why none is sent. Pure."""
    subject = action.subject
    repository, _, number = subject.partition("#")
    if repository != cutover_repository:
        return _refused(
            action,
            EnumTickWriteRefusal.OUT_OF_CUTOVER,
            f"{repository} is not the cutover repository {cutover_repository}",
        )
    kind = action.kind
    if kind is EnumLandingActionKind.MERGE:
        operation = EnumPrLandingGithubOperation.ARM_AUTO_MERGE
    elif kind in _RERUNS:
        operation = EnumPrLandingGithubOperation.RERUN_RUNS
    elif kind is EnumLandingActionKind.UPDATE_BRANCH:
        operation = EnumPrLandingGithubOperation.UPDATE_BRANCH
    else:
        return _refused(
            action,
            EnumTickWriteRefusal.NOT_A_GITHUB_WRITE,
            f"{kind.value} is performed by its own node, not the GitHub landing effect",
        )
    if not number:
        return _refused(
            action, EnumTickWriteRefusal.NOT_A_GITHUB_WRITE, f"{subject} is no PR"
        )
    if action.head_sha is None:
        return _refused(
            action, EnumTickWriteRefusal.NO_HEAD, f"{kind.value} names no head"
        )
    facts = pr_facts.get(subject)
    if facts is None:
        return _refused(
            action, EnumTickWriteRefusal.NO_WRITE_FACTS, f"no tick facts for {subject}"
        )
    if facts.head_sha != action.head_sha:
        return _refused(
            action,
            EnumTickWriteRefusal.HEAD_MOVED,
            f"decided at {action.head_sha[:12]}, read at {facts.head_sha[:12]}",
        )
    fields: dict[str, object] = {
        "correlation_id": correlation_id,
        "mode": mode,
        "repository": repository,
        "pr_number": int(number),
        "head_sha": action.head_sha,
    }
    if operation is EnumPrLandingGithubOperation.ARM_AUTO_MERGE:
        if facts.pr_node_id is None:
            return _refused(
                action, EnumTickWriteRefusal.NO_NODE_ID, f"no node id for {subject}"
            )
        if facts.merge_queue:
            operation = EnumPrLandingGithubOperation.ENQUEUE
        fields["pr_node_id"] = facts.pr_node_id
    elif operation is EnumPrLandingGithubOperation.RERUN_RUNS:
        if not facts.failed_run_ids:
            return _refused(
                action, EnumTickWriteRefusal.NO_RUN_IDS, f"no failed run on {subject}"
            )
        fields["run_ids"] = tuple(sorted(set(facts.failed_run_ids)))
    request = ModelPrLandingGithubRequest.model_validate(
        {**fields, "operation": operation}
    )
    return ModelTickWriteOutcome(action=action, request=request)


def pr_facts_of(facts: ModelLandingFacts) -> dict[str, ModelLandingPrFacts]:
    """The tick's PR facts keyed by PR, as the decision's action subjects name them."""
    return {pr.pr: pr for pr in facts.prs}


class GithubEffectTickWriter:
    """A ProtocolTickWriter that sends each action to the GitHub landing effect over the bus."""

    def __init__(
        self,
        *,
        publisher: ProtocolTickPublisher,
        cutover_repository: str,
        mode: EnumPrLandingGithubMode,
        command_topic: str | None = None,
        correlation_ids: Callable[[], UUID] = uuid4,
    ) -> None:
        self._publisher = publisher
        self._cutover = cutover_repository
        self._mode = mode
        self._topic = command_topic or effect_command_topic()
        self._ids = correlation_ids
        self.outcomes: list[ModelTickWriteOutcome] = []

    def perform(self, action: ModelLandingAction, facts: ModelLandingFacts) -> bool:
        """Send the action's command when the tick's facts pin it; whether one was sent."""
        outcome = plan_write(
            action,
            cutover_repository=self._cutover,
            pr_facts=pr_facts_of(facts),
            mode=self._mode,
            correlation_id=self._ids(),
        )
        self.outcomes.append(outcome)
        if outcome.request is None:
            return False
        self._publisher.publish(self._topic, outcome.request)
        return True


__all__: list[str] = [
    "GithubEffectTickWriter",
    "effect_command_topic",
    "plan_write",
    "pr_facts_of",
]
