# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The side effects a landing transition may ask for."""

from __future__ import annotations

from enum import StrEnum


class EnumPrLandingIntentKind(StrEnum):
    """Every intent named in the transition table, and nothing else.

    ``companion.*`` intents become the autobind command with the matching
    ``op``; ``github.*`` intents become a GitHub landing effect request;
    ``agent_needed`` becomes the agent-needed event. ``github.read_pr_state``
    is issued by the orchestrator itself (on every autobind prompt and on the
    reconciliation tick), never by the reducer: its answer is the snapshot
    that carries the per-PR ordering key (revision 1 of plan 5.1, section 6). The workflow never merges
    (safety property P5), so there is no merge intent.
    """

    COMPANION_DERIVE = "companion.derive"
    COMPANION_REGENERATE = "companion.regenerate"
    COMPANION_VERIFY = "companion.verify"
    GITHUB_ARM = "github.arm"
    GITHUB_ENQUEUE = "github.enqueue"
    GITHUB_DISARM = "github.disarm"
    GITHUB_RERUN = "github.rerun"
    GITHUB_UPDATE_BRANCH = "github.update_branch"
    GITHUB_READ_HEAD_CHECKS = "github.read_head_checks"
    GITHUB_READ_PR_STATE = "github.read_pr_state"
    AGENT_NEEDED = "agent_needed"


# Kinds the orchestrator issues itself and no transition-table row asks for.
ORCHESTRATOR_ISSUED_INTENT_KINDS: frozenset[EnumPrLandingIntentKind] = frozenset(
    {EnumPrLandingIntentKind.GITHUB_READ_PR_STATE}
)


__all__: list[str] = ["ORCHESTRATOR_ISSUED_INTENT_KINDS", "EnumPrLandingIntentKind"]
