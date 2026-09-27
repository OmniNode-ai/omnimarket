# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One ``read_pr_state`` answer to the ordered observations the reducer reads.

Revision 1 of plan 5.1, section 6. The orchestrator is the producer of every
snapshot: it issues a conditional read on each autobind prompt and each
reconciliation tick, and turns the answer into observations whose
``source_seq`` is its own per-PR read sequence. Reads are serialized by the
one-in-flight rule (R4), so read ``n`` answers after read ``n - 1``.

A read coalesces every change since the previous one, so one answer can carry
several facts (a push and a draft, a hold and a title). Each fact becomes its
own observation with its own key, ``read_id * SEQ_STRIDE + i``, all newer than
anything the previous read produced and ordered among themselves:

1. merged, alone; else closed, alone;
2. reopened, when the row is CLOSED (G5), carrying draft and held;
3. pushed, when the head differs, carrying draft and held so the row records
   them at the new head before OBSERVED's evaluation;
4. at the same head: converted_to_draft or ready_for_review, hold_applied or
   hold_lifted, then title_edited when the ticket tokens changed;
5. disarmed, when the row is ARMED by auto-merge and GitHub no longer reports
   an auto-merge request.

The orchestrator appends ``evaluation`` itself whenever a step leaves the row in
OBSERVED; it is not part of the snapshot.
"""

from __future__ import annotations

import re
from datetime import datetime

from omnimarket.events.pr_landing_github.model_github_pr_state_fact import (
    ModelGithubPrStateFact,
)
from omnimarket.events.topics import PR_LANDING_GITHUB_COMPLETED_TOPIC_V1
from omnimarket.merge_control.hold_marker import evaluate_merge_hold
from omnimarket.nodes.node_pr_landing_orchestrator.models.enum_pr_landing_arm_method import (
    EnumPrLandingArmMethod,
)
from omnimarket.nodes.node_pr_landing_orchestrator.models.enum_pr_landing_observation_kind import (
    EnumPrLandingObservationKind,
)
from omnimarket.nodes.node_pr_landing_orchestrator.models.enum_pr_landing_state import (
    EnumPrLandingState,
)
from omnimarket.nodes.node_pr_landing_orchestrator.models.model_pr_landing_observation import (
    ModelPrLandingObservation,
    landing_key,
)
from omnimarket.nodes.node_pr_landing_orchestrator.models.model_pr_landing_state import (
    ModelPrLandingState,
)

# Room for every fact one read can carry (at most five), so read n's keys are
# all greater than read n - 1's.
SEQ_STRIDE = 8

# The ticket token the change-control workflow keys on (the same pattern the
# companion stamp and the autobind publisher use).
TICKET_TOKEN_RE = re.compile(r"\bOMN-\d+\b")


def ticket_ids_in(title: str) -> tuple[str, ...]:
    """Ticket tokens in a title, in order, each once."""
    seen: list[str] = []
    for token in TICKET_TOKEN_RE.findall(title):
        if token not in seen:
            seen.append(token)
    return tuple(seen)


def is_held(fact: ModelGithubPrStateFact) -> bool:
    """A hold marker in the title or a label, by the one hold vocabulary."""
    decision = evaluate_merge_hold(title=fact.title, labels=fact.labels)
    return not decision.is_merge_eligible


def snapshot_observations(
    *,
    landing: ModelPrLandingState | None,
    fact: ModelGithubPrStateFact,
    repository: str,
    read_id: int,
    observed_at: datetime,
    source_event_id: str,
) -> tuple[ModelPrLandingObservation, ...]:
    """The observations one snapshot yields for this row, in apply order."""
    held = is_held(fact)
    tickets = ticket_ids_in(fact.title)
    base_seq = read_id * SEQ_STRIDE
    out: list[ModelPrLandingObservation] = []

    def observe(
        kind: EnumPrLandingObservationKind,
        *,
        head: str | None,
        flags: bool = False,
    ) -> None:
        out.append(
            ModelPrLandingObservation(
                repository=repository,
                pr_number=fact.pr_number,
                landing_key=landing_key(repository, fact.pr_number),
                head_sha=head,
                kind=kind,
                observed_at=observed_at,
                source_topic=PR_LANDING_GITHUB_COMPLETED_TOPIC_V1,
                source_event_id=f"{source_event_id}:{len(out)}",
                ticket_ids=tickets,
                source_seq=base_seq + len(out) + 1,
                draft=fact.draft if flags else None,
                held=held if flags else None,
            )
        )

    kinds = EnumPrLandingObservationKind
    if fact.merged:
        if landing is not None:
            observe(kinds.MERGED, head=fact.head_sha)
        return tuple(out)
    if fact.state == "closed":
        if landing is not None:
            observe(kinds.CLOSED, head=fact.head_sha)
        return tuple(out)
    if landing is None:
        # First sight: the push that creates the row, with its draft and hold.
        observe(kinds.PUSHED, head=fact.head_sha, flags=True)
        return tuple(out)
    if landing.state is EnumPrLandingState.CLOSED:
        observe(kinds.REOPENED, head=fact.head_sha, flags=True)
        return tuple(out)
    if fact.head_sha != landing.head_sha:
        observe(kinds.PUSHED, head=fact.head_sha, flags=True)
        return tuple(out)
    head = landing.head_sha
    if fact.draft and not landing.draft:
        observe(kinds.CONVERTED_TO_DRAFT, head=head)
    elif landing.draft and not fact.draft:
        observe(kinds.READY_FOR_REVIEW, head=head)
    if held and not landing.held:
        observe(kinds.HOLD_APPLIED, head=head)
    elif landing.held and not held:
        observe(kinds.HOLD_LIFTED, head=head)
    if tickets != landing.ticket_ids:
        observe(kinds.TITLE_EDITED, head=head)
    if (
        landing.state is EnumPrLandingState.ARMED
        and landing.armed is EnumPrLandingArmMethod.AUTO_MERGE
        and not fact.auto_merge_armed
    ):
        observe(kinds.DISARMED, head=head)
    return tuple(out)


__all__: list[str] = [
    "SEQ_STRIDE",
    "TICKET_TOKEN_RE",
    "is_held",
    "snapshot_observations",
    "ticket_ids_in",
]
