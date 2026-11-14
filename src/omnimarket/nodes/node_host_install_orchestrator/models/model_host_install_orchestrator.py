# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed request, working memory and terminal event of the host unit install orchestrator (OMN-23957)."""

from __future__ import annotations

from enum import StrEnum
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.models.host_install_state import (
    EnumHostInstallState,
    ModelHostInstallRecord,
    ModelHostInstallStepEvent,
)
from omnimarket.models.host_installer_registry_units import (
    ModelRegistryUnitsRequest,
)


class EnumHostInstallPhase(StrEnum):
    """The step whose result the orchestrator is waiting for."""

    RENDER = "render"
    SNAPSHOT = "snapshot"
    INSTALL = "install"
    READ = "read"
    READBACK = "readback"
    RUN = "run"
    ROLLBACK = "rollback"


class EnumHostInstallNext(StrEnum):
    """What the orchestrator does when the reducer's record for the step it published comes back."""

    SNAPSHOT = "snapshot"
    INSTALL = "install"
    READ = "read"
    STATUS = "status"
    RESTORE = "restore"
    FINISH = "finish"


class ModelHostInstallRequest(BaseModel):
    """One install of one registry unit on one host. ``registry_request.mode`` is ignored: each step sets its own."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: UUID
    host: str = Field(min_length=1)
    unit: str = Field(min_length=1)
    actor: str = Field(min_length=1)
    platform: Literal["systemd", "launchd"]
    label: str = Field(min_length=1)
    registry_request: ModelRegistryUnitsRequest


class ModelHostInstallCompleted(BaseModel):
    """The terminal event: how the install ended, with the digests the reducer holds."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: UUID
    host: str
    unit: str
    final_state: EnumHostInstallState
    outcome: Literal["installed", "rolled_back", "failed"]
    render_digest: str = ""
    installed_digest: str = ""
    rollback_target_digest: str = ""
    detail: str = ""


class ModelHostInstallContext(BaseModel):
    """The bounded working memory of one install: the request, the phase awaited, and what follows the next record."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    request: ModelHostInstallRequest
    phase: EnumHostInstallPhase
    render_text: str = ""
    render_digest: str = ""
    prior: ModelHostInstallRecord | None = None
    pending_steps: tuple[ModelHostInstallStepEvent, ...] = ()
    next_action: EnumHostInstallNext = EnumHostInstallNext.SNAPSHOT


__all__: list[str] = [
    "EnumHostInstallNext",
    "EnumHostInstallPhase",
    "ModelHostInstallCompleted",
    "ModelHostInstallContext",
    "ModelHostInstallRequest",
]
