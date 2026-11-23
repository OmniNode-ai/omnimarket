# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Failed-roll hold bound and held-command age alarm of node_dev_roll_window_gate_effect (OMN-26484).

Two pure rules the tick handler applies, both from overlay values (nothing here is a bound of its own):

* The failed-roll hold is counted from the roll's ``runtime_started_at``. The batching decision releases
  after ``finished_at`` plus ``failed_roll_release_after_seconds``, so a roll that ran for an hour before it
  finished (or was given up on) held the commands twice as long as the declared bound. ``bounded_last_roll``
  hands the decision the same record with ``finished_at`` no later than the start, which moves the release
  to ``runtime_started_at`` plus the declared seconds; the roll completion deadline is likewise never later
  than that bound (``abandon_after_seconds``).
* A held command older than ``held_age_alarm_after_seconds`` raises one alarm naming the roll it waits behind
  and the aged held commands. The alarm is the lab alarm delivery's own request
  (node_lab_alarm_deliver_effect, which appends one ledger row per episode and, where the deployment's
  overlay routes the condition to an owner lane, one MSG row to that lane); the once-only record is the
  gate's own state (``alarmed_ids``), so a tick, a redelivered tick or a restarted runtime does not repeat it.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID, uuid5

from omnimarket.models.dev_roll import EnumDevRollHealth, ModelDevRollRecord
from omnimarket.models.lab_alarm_delivery import (
    EnumDeliveryCondition,
    ModelLabAlarm,
    ModelLabAlarmDeliveryRequest,
)
from omnimarket.nodes.node_dev_roll_window_gate_effect.models import (
    ModelDevRollGateState,
    ModelDevRollWindowGatePolicy,
    ModelHeldDevRoll,
)

#: The condition name the lab alarm delivery keys its episodes on.
ALARM_CONDITION = "dev_roll_held_command_age"
#: No roll was released that the aged commands could be waiting behind.
NO_ROLL = "none"
_REQUEST_NAMESPACE = UUID("5f0e6d9c-26a4-4b84-9c0e-0d7a4c3b9e21")


def abandon_after_seconds(policy: ModelDevRollWindowGatePolicy) -> int:
    """Seconds after the roll's start at which an unfinished roll is given up on: never past the release bound."""
    return min(
        policy.roll_completion_deadline_seconds,
        policy.failed_roll_release_after_seconds,
    )


def bounded_last_roll(last: ModelDevRollRecord | None) -> ModelDevRollRecord | None:
    """The last roll as the batching decision should read it: a failed roll's hold counts from its start.

    Only a finished roll the decision treats as failed (not healthy, or healthy with no ``healthy_since``) is
    changed, and only its ``finished_at``, which the decision reads for nothing else once the roll has finished.
    """
    if (
        last is None
        or last.runtime_started_at is None
        or last.finished_at is None
        or last.finished_at <= last.runtime_started_at
    ):
        return last
    if last.health is EnumDevRollHealth.HEALTHY and last.healthy_since is not None:
        return last
    return last.model_copy(update={"finished_at": last.runtime_started_at})


def _line(held: ModelHeldDevRoll, now: datetime) -> str:
    age = int((now - held.held_at).total_seconds())
    return f"{held.command.requested_by} ({held.command.correlation_id}, held {age} s)"


def held_age_alarm(
    state: ModelDevRollGateState,
    blocking_roll: ModelDevRollRecord | None,
    policy: ModelDevRollWindowGatePolicy,
    now: datetime,
) -> tuple[ModelLabAlarmDeliveryRequest | None, tuple[str, ...]]:
    """The alarm request (if a held command grew old and was not yet alarmed) and the alarmed ids to keep.

    ``blocking_roll`` is the roll the held commands waited behind when the tick began. The alarm names every
    held command older than the bound; it is raised only when one of them has not been alarmed before, so a
    command that grows old later raises a new alarm that names it, and nothing repeats.
    """
    bound = policy.held_age_alarm_after_seconds
    if bound is None:
        return None, tuple(state.alarmed_ids)
    aged = sorted(
        (h for h in state.held if (now - h.held_at).total_seconds() > bound),
        key=lambda h: (h.held_at, str(h.command.correlation_id)),
    )
    held_ids = {str(h.command.correlation_id) for h in state.held}
    kept = tuple(i for i in state.alarmed_ids if i in held_ids)
    aged_ids = tuple(str(h.command.correlation_id) for h in aged)
    if not any(i not in state.alarmed_ids for i in aged_ids):
        return None, kept
    roll_id = NO_ROLL if blocking_roll is None else blocking_roll.job_id
    subject = f"roll {roll_id} holds {', '.join(aged_ids)}"
    detail = (
        f"{len(aged)} dev deploy command(s) held more than "
        f"{bound} s behind roll {roll_id}: " + "; ".join(_line(h, now) for h in aged)
    )
    request = ModelLabAlarmDeliveryRequest(
        correlation_id=str(uuid5(_REQUEST_NAMESPACE, subject)),
        condition=ALARM_CONDITION,
        outcome=EnumDeliveryCondition.ALARM,
        alarms=(
            ModelLabAlarm(condition=ALARM_CONDITION, subject=subject, detail=detail),
        ),
        evidence=(
            f"dev roll window gate: roll {roll_id}, observed_at {now.isoformat()}, "
            f"held_age_alarm_after_seconds {bound}, "
            f"held {len(state.held)}, aged {len(aged)}"
        ),
    )
    return request, tuple(dict.fromkeys((*kept, *aged_ids)))
