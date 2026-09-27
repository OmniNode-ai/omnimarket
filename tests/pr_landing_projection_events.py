# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Wire builders for the four landing events, shared by the OMN-19833 tests.

Every builder goes through the frozen T2 payload class and dumps it in JSON
mode, so a test event is exactly what the orchestrator will put on the bus,
plus the underscore-prefixed keys the runtime injects beside it.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from omnimarket.events.pr_landing.enum_pr_landing_agent_reason import (
    EnumPrLandingAgentReason,
)
from omnimarket.events.pr_landing.enum_pr_landing_intent_kind import (
    EnumPrLandingIntentKind,
)
from omnimarket.events.pr_landing.enum_pr_landing_state import (
    EnumPrLandingState,
)
from omnimarket.events.pr_landing.model_pr_landing_agent_needed import (
    ModelPrLandingAgentNeeded,
)
from omnimarket.events.pr_landing.model_pr_landing_closed import (
    ModelPrLandingClosed,
)
from omnimarket.events.pr_landing.model_pr_landing_intent import (
    ModelPrLandingIntent,
)
from omnimarket.events.pr_landing.model_pr_landing_merged import (
    ModelPrLandingMerged,
)
from omnimarket.events.pr_landing.model_pr_landing_transitioned import (
    ModelPrLandingTransitioned,
)
from omnimarket.events.topics import (
    PR_LANDING_AGENT_NEEDED_TOPIC_V1,
    PR_LANDING_CLOSED_TOPIC_V1,
    PR_LANDING_MERGED_TOPIC_V1,
    PR_LANDING_TRANSITIONED_TOPIC_V1,
)

T0 = datetime(2026, 9, 27, 7, 0, 0, tzinfo=UTC)
REPO = "OmniNode-ai/omnimarket"
PR = 2983
HEAD_A = "a" * 40
HEAD_B = "b" * 40


def at(seq: int) -> datetime:
    """Event time for a seq: later seq, later time, so time never decides order."""
    return T0 + timedelta(seconds=seq)


def _wire(model: Any, topic: str, offset: int) -> dict[str, Any]:
    payload: dict[str, Any] = model.model_dump(mode="json")
    payload.update(
        {
            "_topic": topic,
            "_partition": 0,
            "_offset": offset,
            "_fallback_id": f"omn19833-{offset}",
            "_envelope_timestamp": T0.isoformat(),
        }
    )
    return payload


def transitioned(
    seq: int,
    from_state: EnumPrLandingState | None,
    to_state: EnumPrLandingState,
    trigger: str,
    *,
    head_sha: str | None = HEAD_A,
    arm: bool = False,
    time: datetime | None = None,
) -> dict[str, Any]:
    intents: tuple[ModelPrLandingIntent, ...] = ()
    if arm:
        intents = (
            ModelPrLandingIntent(
                kind=EnumPrLandingIntentKind.GITHUB_ARM,
                repository=REPO,
                pr_number=PR,
                head_sha=head_sha,
            ),
        )
    event = ModelPrLandingTransitioned(
        repository=REPO,
        pr_number=PR,
        head_sha=head_sha,
        seq=seq,
        from_state=from_state,
        to_state=to_state,
        trigger=trigger,
        intents=intents,
        transitioned_at=time or at(seq),
    )
    return _wire(event, PR_LANDING_TRANSITIONED_TOPIC_V1, seq * 10)


def agent_needed(
    seq: int,
    reason: EnumPrLandingAgentReason = EnumPrLandingAgentReason.REAL_RED,
    *,
    from_state: EnumPrLandingState = EnumPrLandingState.CHECKS_PENDING,
    detail: str | None = "lint",
    head_sha: str = HEAD_A,
) -> dict[str, Any]:
    event = ModelPrLandingAgentNeeded(
        repository=REPO,
        pr_number=PR,
        head_sha=head_sha,
        reason=reason,
        from_state=from_state,
        detail=detail,
        seq=seq,
        raised_at=at(seq),
    )
    return _wire(event, PR_LANDING_AGENT_NEEDED_TOPIC_V1, seq * 10 + 1)


def merged(seq: int, episode: int, *, head_sha: str = HEAD_A) -> dict[str, Any]:
    event = ModelPrLandingMerged(
        repository=REPO,
        pr_number=PR,
        head_sha=head_sha,
        seq=seq,
        episode=episode,
        merged_at=at(seq),
    )
    return _wire(event, PR_LANDING_MERGED_TOPIC_V1, seq * 10 + 2)


def closed(seq: int, episode: int) -> dict[str, Any]:
    event = ModelPrLandingClosed(
        repository=REPO,
        pr_number=PR,
        head_sha=HEAD_A,
        seq=seq,
        episode=episode,
        closed_at=at(seq),
    )
    return _wire(event, PR_LANDING_CLOSED_TOPIC_V1, seq * 10 + 3)


S = EnumPrLandingState


def a_reopened_pr_life() -> list[dict[str, Any]]:
    """Opened, closed unmerged, reopened on a new head, armed and merged.

    Two episodes and two terminals, the F10 case. The events of one transition
    share its seq.
    """
    return [
        transitioned(1, None, S.OBSERVED, "pushed"),
        transitioned(2, S.OBSERVED, S.CHECKS_PENDING, "evaluated_checks_required"),
        transitioned(3, S.CHECKS_PENDING, S.CLOSED, "closed"),
        closed(3, 0),
        transitioned(4, S.CLOSED, S.OBSERVED, "reopened", head_sha=HEAD_B),
        transitioned(
            5,
            S.OBSERVED,
            S.CHECKS_PENDING,
            "evaluated_checks_required",
            head_sha=HEAD_B,
        ),
        transitioned(
            6, S.CHECKS_PENDING, S.NEEDS_AGENT, "verdict_real_red", head_sha=HEAD_B
        ),
        agent_needed(6, head_sha=HEAD_B),
        transitioned(7, S.NEEDS_AGENT, S.OBSERVED, "pushed", head_sha=HEAD_B),
        transitioned(
            8,
            S.OBSERVED,
            S.CHECKS_PENDING,
            "evaluated_checks_required",
            head_sha=HEAD_B,
        ),
        transitioned(
            9, S.CHECKS_PENDING, S.READY, "verdict_green", head_sha=HEAD_B, arm=True
        ),
        transitioned(10, S.READY, S.ARMED, "armed_confirmed", head_sha=HEAD_B),
        transitioned(11, S.ARMED, S.MERGED, "merged", head_sha=HEAD_B),
        merged(11, 1, head_sha=HEAD_B),
    ]


__all__ = [
    "HEAD_A",
    "HEAD_B",
    "PR",
    "REPO",
    "T0",
    "S",
    "a_reopened_pr_life",
    "agent_needed",
    "at",
    "closed",
    "merged",
    "transitioned",
]
