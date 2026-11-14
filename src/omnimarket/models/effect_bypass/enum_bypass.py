# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The closed vocabularies of the effect-node bypass ratchet.

A bypass is an effect on the outside world that does not go through its effect node over the bus
(RULING 2026-11-14T11:59:02Z, OMN-24432). Each category names one kind of outside world.
"""

from __future__ import annotations

from enum import StrEnum


class EnumBypassCategory(StrEnum):
    """One kind of outside-world call that must go through an effect node."""

    GH_CLI = "gh_cli"
    GITHUB_API = "github_api"
    GIT_REMOTE = "git_remote"
    SLACK_API = "slack_api"
    LINEAR_API = "linear_api"
    HOST_SSH = "host_ssh"
    DOCKER_ADMIN = "docker_admin"
    KAFKA_ADMIN = "kafka_admin"


class EnumBypassFinding(StrEnum):
    """Why the ratchet refused, or what it noted."""

    CATEGORY_ROSE = "category_rose"
    REPOSITORY_NOT_IN_BASELINE = "repository_not_in_baseline"
    BASELINE_GREW_VS_BASE = "baseline_grew_vs_base"
    BASELINE_LOWERABLE = "baseline_lowerable"
