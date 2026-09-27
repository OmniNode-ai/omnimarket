# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The in-row outbox rules (plan 5.1 revision 1, section 3; F6, F8, F9, R4).

Pure functions over the outbox tuple the reducer's row carries. The
orchestrator writes intents here in the same compare-and-set that writes the
row, and removes an entry through that same compare-and-set when it sends it
(F8), so a stale write can never bring back an entry already sent.

* F6: a disarm added to the outbox removes an unsent arm or enqueue of the
  product PR, and an arm or enqueue removes an unsent disarm. The disarm itself
  is kept: an arm may already be confirmed on GitHub from an earlier entry.
* R4: one GitHub effect per PR in flight. :func:`next_sendable` picks the entry
  to send only when nothing is in flight; the caller enforces that.
* F9: every effect is sent with a correlation id derived from the row key, the
  dispatch number and the intent, so a re-sent entry (at-least-once delivery,
  or a compare-and-set retry that re-runs the leg) carries the same id and the
  effect deduplicates on it.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from uuid import UUID, uuid5

from omnimarket.nodes.node_pr_landing_orchestrator.models.enum_pr_landing_intent_kind import (
    EnumPrLandingIntentKind,
)
from omnimarket.nodes.node_pr_landing_orchestrator.models.model_pr_landing_intent import (
    ModelPrLandingIntent,
)

# Namespace of every id the landing orchestrator derives. Fixed forever: ids
# derived under it are the deduplication keys effects and consumers store.
PR_LANDING_NAMESPACE = UUID("5f0c3a8e-19c9-4b2e-9d6a-2a1f7c4e1982")

GITHUB_EFFECT_KINDS: frozenset[EnumPrLandingIntentKind] = frozenset(
    {
        EnumPrLandingIntentKind.GITHUB_ARM,
        EnumPrLandingIntentKind.GITHUB_ENQUEUE,
        EnumPrLandingIntentKind.GITHUB_DISARM,
        EnumPrLandingIntentKind.GITHUB_RERUN,
        EnumPrLandingIntentKind.GITHUB_UPDATE_BRANCH,
        EnumPrLandingIntentKind.GITHUB_READ_HEAD_CHECKS,
        EnumPrLandingIntentKind.GITHUB_READ_PR_STATE,
    }
)

_ARM_KINDS = frozenset(
    {EnumPrLandingIntentKind.GITHUB_ARM, EnumPrLandingIntentKind.GITHUB_ENQUEUE}
)


def is_product_arm(intent: ModelPrLandingIntent) -> bool:
    """An arm or enqueue of the product PR itself (not of its companion)."""
    return intent.kind in _ARM_KINDS and intent.target_pr is None


def _is_disarm(intent: ModelPrLandingIntent) -> bool:
    return intent.kind is EnumPrLandingIntentKind.GITHUB_DISARM


def add_intent(
    outbox: tuple[ModelPrLandingIntent, ...], intent: ModelPrLandingIntent
) -> tuple[ModelPrLandingIntent, ...]:
    """Queue one GitHub effect under F6. An identical queued entry is not added twice."""
    if intent.kind not in GITHUB_EFFECT_KINDS:
        msg = f"{intent.kind.value} is not a GitHub effect and never enters the outbox"
        raise ValueError(msg)
    kept = outbox
    if _is_disarm(intent):
        kept = tuple(entry for entry in kept if not is_product_arm(entry))
    elif is_product_arm(intent):
        kept = tuple(entry for entry in kept if not _is_disarm(entry))
    if intent in kept:
        return kept
    return (*kept, intent)


def add_intents(
    outbox: tuple[ModelPrLandingIntent, ...],
    intents: tuple[ModelPrLandingIntent, ...],
) -> tuple[ModelPrLandingIntent, ...]:
    """Queue several GitHub effects in order, each under F6."""
    for intent in intents:
        outbox = add_intent(outbox, intent)
    return outbox


def is_deferred(
    intent: ModelPrLandingIntent,
    *,
    head_checks_read_at: datetime | None,
    poll_interval: timedelta,
    now: datetime,
) -> bool:
    """A head-check read waits out the state's poll interval since the last one."""
    if intent.kind is not EnumPrLandingIntentKind.GITHUB_READ_HEAD_CHECKS:
        return False
    if head_checks_read_at is None:
        return False
    return now - head_checks_read_at < poll_interval


def next_sendable(
    outbox: tuple[ModelPrLandingIntent, ...],
    *,
    head_checks_read_at: datetime | None,
    poll_interval: timedelta,
    now: datetime,
) -> int | None:
    """Index of the first entry that may be sent now, or None.

    Entries go out in the order they were queued. A head-check read still
    inside its poll interval is skipped, not waited on, so a disarm queued
    behind it is not held up.
    """
    for index, intent in enumerate(outbox):
        if not is_deferred(
            intent,
            head_checks_read_at=head_checks_read_at,
            poll_interval=poll_interval,
            now=now,
        ):
            return index
    return None


def effect_correlation_id(
    landing_key: str, dispatch_number: int, intent: ModelPrLandingIntent
) -> UUID:
    """The deterministic id one sent effect carries (F9)."""
    parts = (
        landing_key,
        str(dispatch_number),
        intent.kind.value,
        intent.head_sha or "-",
        str(intent.target_pr or "-"),
        ",".join(intent.check_runs),
    )
    return uuid5(PR_LANDING_NAMESPACE, "|".join(parts))


def companion_command_id(landing_key: str, reducer_command_id: str) -> UUID:
    """The autobind command's correlation id for a reducer-issued companion command.

    The producer answers with this id (T10), and the row records it as the
    command in flight (F4, F5). A reducer id that is already a UUID is used as
    is, so the row and the wire agree without a lookup.
    """
    try:
        return UUID(reducer_command_id)
    except ValueError:
        return uuid5(
            PR_LANDING_NAMESPACE, f"{landing_key}|command|{reducer_command_id}"
        )


__all__: list[str] = [
    "GITHUB_EFFECT_KINDS",
    "PR_LANDING_NAMESPACE",
    "add_intent",
    "add_intents",
    "companion_command_id",
    "effect_correlation_id",
    "is_deferred",
    "is_product_arm",
    "next_sendable",
]
