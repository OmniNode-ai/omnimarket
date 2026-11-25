# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Trigger, overlay settings and result of the install-silence alert effect (OMN-26658)."""

from __future__ import annotations

from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, PositiveInt

from omnimarket.nodes.node_hook_install_emission_compute.models import (
    ModelHookInstallAlert,
    ModelHookInstallRequiredKind,
)
from omnimarket.nodes.node_hook_install_emission_compute.models.model_hook_install_emission import (
    Tool,
)


class EnumSilenceOutcome(StrEnum):
    QUIET = "quiet"
    ALERTED = "alerted"
    UNREADABLE = "unreadable"
    THROTTLED = "throttled"


class ModelHookInstallSilenceTrigger(BaseModel):
    """A heartbeat carries the trigger; foreign payload fields are ignored."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    correlation_id: UUID | None = None


class ModelOwnedInstall(BaseModel):
    """One install on a machine we own: the host, the tool and the names the host reports."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    host: str = Field(min_length=1)
    tool: Tool
    host_aliases: tuple[str, ...] = ()


class ModelHookInstallSilenceSettings(BaseModel):
    """The overlay document: the installs we own and the windows. Every field is a deployment decision."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    installs: tuple[ModelOwnedInstall, ...] = Field(min_length=1)
    required_kinds: tuple[ModelHookInstallRequiredKind, ...] = Field(min_length=1)
    lookback_minutes: PositiveInt
    quiet_minutes: PositiveInt = 30
    check_interval_seconds: PositiveInt


class ModelHookInstallSilenceResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    outcome: EnumSilenceOutcome
    alerts: tuple[ModelHookInstallAlert, ...] = ()
    alert_text: str = ""
    reason: str = ""
    alarm_outcome: str = ""
