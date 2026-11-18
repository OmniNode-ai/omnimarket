# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Why the gate did not hold a deploy command."""

from enum import StrEnum


class EnumDevRollGateRefusal(StrEnum):
    """One typed reason per refused hold; nothing is stored for a refused command.

    Every reason but ``already_released`` passes the command straight on to the deploy
    agent: the gate never drops a command it does not hold.
    """

    NOT_A_DEV_COMMAND = "not_a_dev_command"
    REQUESTER_NOT_A_CI_ISSUER = "requester_not_a_ci_issuer"
    GIT_REF_NOT_A_SHA = "git_ref_not_a_sha"
    REPOSITORY_UNKNOWN = "repository_unknown"
    PINNED_IMAGE = "pinned_image"
    ALREADY_RELEASED = "already_released"
    HELD_FULL = "held_full"
    GATE_OFF = "gate_off"
