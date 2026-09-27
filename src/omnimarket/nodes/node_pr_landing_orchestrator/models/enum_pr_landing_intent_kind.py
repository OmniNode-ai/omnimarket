# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The side effects a landing transition may ask for."""

from __future__ import annotations

from enum import StrEnum


class EnumPrLandingIntentKind(StrEnum):
    """Every intent named in the transition table, and nothing else.

    ``companion.*`` intents become the autobind command with the matching
    ``op``; ``github.*`` intents become a GitHub landing effect request;
    ``agent_needed`` becomes the agent-needed event. The workflow never merges
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
    AGENT_NEEDED = "agent_needed"


__all__: list[str] = ["EnumPrLandingIntentKind"]
