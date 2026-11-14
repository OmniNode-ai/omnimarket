# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One dev deploy command the deploy effect hands to the roll gate to hold."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from omnimarket.events.runtime_deployment import ModelDeployPublishCommand


class ModelDevRollHoldRequest(BaseModel):
    """The deploy effect's forward: the whole command, unchanged.

    Topic ``onex.cmd.omnimarket.dev-roll-hold-requested.v1``. The gate reads
    ``correlation_id``, ``requested_by``, ``git_ref`` and ``requested_at`` from the
    command; the seam test pins those names against the command model.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    command: ModelDeployPublishCommand
