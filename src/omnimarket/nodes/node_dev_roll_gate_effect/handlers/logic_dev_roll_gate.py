# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The gate's state transitions, shared by its five handlers (OMN-24431).

Every function here is pure: the clock is an argument, the state and the policy are
values, nothing is read or sent. Handlers load the state, call these, save and answer.
"""

from __future__ import annotations

import hashlib
import re
from datetime import datetime, timedelta
from uuid import UUID, uuid5

from omnimarket.events.runtime_deployment import ModelDeployPublishCommand
from omnimarket.models.dev_roll import (
    EnumDevRollHealth,
    ModelDevRollFacts,
    ModelDevRollRecord,
    ModelDevTipChanged,
)
from omnimarket.nodes.node_dev_roll_gate_effect.models import (
    ModelDevRollGatePolicy,
    ModelDevRollGateState,
    ModelDevRollHeldCommand,
)

NAMESPACE = UUID("0b6a2f8e-5d0c-4e0e-9d57-6a1d3a2c8f41")

_SHA_RE = re.compile(r"^[0-9a-f]{40}$")


def tip_event(
    held: ModelDevRollHeldCommand, policy: ModelDevRollGatePolicy
) -> ModelDevTipChanged:
    """The batching node's view of one held command.

    The repository comes from the requester, the sha from the ref; a command that names
    neither still counts as one pending change (the overlay's ``unmatched_repository``
    and a sha derived from its correlation id) so it is held and released, never dropped.
    """
    command = held.command
    match = re.fullmatch(policy.issuer_repository_pattern, command.requested_by)
    repository = (
        match.group("repository") if match is not None else policy.unmatched_repository
    )
    ref = command.git_ref
    sha = (
        ref
        if ref is not None and _SHA_RE.fullmatch(ref)
        else hashlib.sha1(
            command.correlation_id.bytes, usedforsecurity=False
        ).hexdigest()
    )
    return ModelDevTipChanged(
        event_id=command.correlation_id,
        repository=repository,
        sha=sha,
        merged_at=held.held_at,
    )


def settle_timeout(
    state: ModelDevRollGateState, policy: ModelDevRollGatePolicy, now: datetime
) -> ModelDevRollGateState:
    """End a roll the deploy agent never answered, after the overlay's bound.

    The roll then reads finished with unknown health, so the batching node holds on
    ``previous_roll_not_healthy`` until its own release bound instead of waiting forever.
    """
    last = state.last_roll
    if not state.outstanding or last is None or last.runtime_started_at is None:
        return state
    deadline = last.runtime_started_at + timedelta(
        seconds=policy.roll_unfinished_after_seconds
    )
    if now < deadline:
        return state
    return state.model_copy(
        update={
            "outstanding": (),
            "succeeded": False,
            "last_roll": last.model_copy(
                update={
                    "finished_at": deadline,
                    "health": EnumDevRollHealth.UNKNOWN,
                    "healthy_since": None,
                }
            ),
        }
    )


def build_facts(
    state: ModelDevRollGateState,
    policy: ModelDevRollGatePolicy,
    now: datetime,
    cause: str,
) -> ModelDevRollFacts:
    """The decision request for the commands held now; ``cause`` seeds its id."""
    return ModelDevRollFacts(
        correlation_id=uuid5(NAMESPACE, f"{cause}/{now.isoformat()}"),
        observed_at=now,
        pending_events=tuple(tip_event(h, policy) for h in state.held),
        last_roll=state.last_roll,
    )


def with_request(
    state: ModelDevRollGateState,
    policy: ModelDevRollGatePolicy,
    facts: ModelDevRollFacts,
) -> ModelDevRollGateState:
    """Remember the decision request, so only its own decision releases anything."""
    kept = (*state.requested_correlation_ids, str(facts.correlation_id))
    return state.model_copy(
        update={"requested_correlation_ids": kept[-policy.ids_kept :]}
    )


def hold(
    state: ModelDevRollGateState,
    policy: ModelDevRollGatePolicy,
    command: ModelDeployPublishCommand,
    now: datetime,
) -> ModelDevRollGateState:
    """Add one command to the held set."""
    key = str(command.correlation_id)
    seen = (*state.seen_command_ids, key)[-policy.ids_kept :]
    return state.model_copy(
        update={
            "held": (
                *state.held,
                ModelDevRollHeldCommand(command=command, held_at=now),
            ),
            "seen_command_ids": seen,
        }
    )


def release(
    state: ModelDevRollGateState,
    chosen: tuple[ModelDevRollHeldCommand, ...],
    git_ref: str,
    release_id: UUID,
    now: datetime,
) -> ModelDevRollGateState:
    """Move ``chosen`` out of the held set and start the roll that carries them."""
    chosen_ids = {str(h.command.correlation_id) for h in chosen}
    return state.model_copy(
        update={
            "held": tuple(
                h for h in state.held if str(h.command.correlation_id) not in chosen_ids
            ),
            "outstanding": tuple(str(h.command.correlation_id) for h in chosen),
            "succeeded": False,
            "last_roll": ModelDevRollRecord(
                job_id=str(release_id),
                git_ref=git_ref,
                runtime_started_at=now,
                finished_at=None,
                health=EnumDevRollHealth.UNKNOWN,
                healthy_since=None,
            ),
        }
    )


def settle_command(
    state: ModelDevRollGateState,
    correlation_id: str,
    *,
    succeeded: bool,
    started_at: datetime | None,
    at: datetime,
) -> tuple[ModelDevRollGateState, bool, bool]:
    """Record one terminal event for a released command.

    Returns the new state, whether the command was one of the outstanding ones, and
    whether that ended the roll. The roll is healthy when a completion with a success
    status arrived for the batch, unhealthy otherwise (all rejected or failed).
    """
    if correlation_id not in state.outstanding or state.last_roll is None:
        return state, False, False
    outstanding = tuple(c for c in state.outstanding if c != correlation_id)
    ok = state.succeeded or succeeded
    last = state.last_roll
    if outstanding:
        return (
            state.model_copy(update={"outstanding": outstanding, "succeeded": ok}),
            True,
            False,
        )
    begun = last.runtime_started_at
    if started_at is not None and started_at <= at:
        begun = started_at
    finished = at if begun is None or at >= begun else begun
    return (
        state.model_copy(
            update={
                "outstanding": (),
                "succeeded": ok,
                "last_roll": last.model_copy(
                    update={
                        "runtime_started_at": begun,
                        "finished_at": finished,
                        "health": EnumDevRollHealth.HEALTHY
                        if ok
                        else EnumDevRollHealth.UNHEALTHY,
                        "healthy_since": finished if ok else None,
                    }
                ),
            }
        ),
        True,
        True,
    )
