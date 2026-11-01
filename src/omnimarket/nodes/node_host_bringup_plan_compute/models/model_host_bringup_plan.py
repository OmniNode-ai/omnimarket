# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed models for the host bring-up plan compute node (OMN-21883).

Every model is frozen and forbids extra keys. A target row is complete only with
a read-only check, a falsifier and an enum action: a row missing any one of them
fails validation, so a contract carrying such a row fails to load.

Rows name things symbolically. A check subject is a lowercase identifier the
host profile resolves later, so a row cannot carry a host name, an address, a
filesystem path or a version string.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

_IDENTIFIER = r"^[a-z][a-z0-9_]*$"
_STAGE = r"^[A-Z]$"


class EnumBringupCheckKind(StrEnum):
    """The read-only already-satisfied probe a target row declares."""

    COMMAND_SUCCEEDS = "command_succeeds"
    PACKAGE_INSTALLED = "package_installed"
    FILE_PRESENT = "file_present"
    SERVICE_ACTIVE = "service_active"
    PORT_LISTENING = "port_listening"
    ENDPOINT_HEALTHY = "endpoint_healthy"
    SECRET_PRESENT = "secret_present"
    PERSON_CONFIRMED = "person_confirmed"


class EnumBringupAction(StrEnum):
    """What the bring-up does when a target is not already satisfied."""

    INSTALL_PACKAGE = "install_package"
    WRITE_FILE = "write_file"
    ENABLE_SERVICE = "enable_service"
    RUN_COMMAND = "run_command"
    PULL_IMAGE = "pull_image"
    START_STACK = "start_stack"
    FETCH_SECRET = "fetch_secret"
    REGISTER_HOST = "register_host"
    PERSON_STEP = "person_step"


class EnumObservedState(StrEnum):
    """What a check observed on the host. Anything not observed is UNKNOWN."""

    OK = "ok"
    NOT_OK = "not_ok"
    UNKNOWN = "unknown"


class EnumStepDecision(StrEnum):
    """Per target: skip only when its check observed OK, otherwise act."""

    SKIP = "skip"
    ACT = "act"


class EnumPlanStatus(StrEnum):
    PLANNED = "planned"
    REFUSED = "refused"


class EnumRefusalKind(StrEnum):
    CYCLE = "cycle"
    UNKNOWN_NEED = "unknown_need"
    UNREACHED_TARGET = "unreached_target"
    NO_TOP_TARGET = "no_top_target"
    DUPLICATE_TARGET = "duplicate_target"
    UNKNOWN_OBSERVED_TARGET = "unknown_observed_target"


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class ModelBringupCheck(_Frozen):
    """A read-only already-satisfied check: its kind and the symbolic subject it probes."""

    kind: EnumBringupCheckKind
    subject: str = Field(pattern=_IDENTIFIER)


class ModelBringupTarget(_Frozen):
    """One row of the bring-up graph."""

    name: str = Field(pattern=_IDENTIFIER)
    stage: str = Field(pattern=_STAGE)
    top: bool = False
    needs: tuple[str, ...] = ()
    check: ModelBringupCheck
    falsifier: str = Field(min_length=1)
    action: EnumBringupAction

    @model_validator(mode="after")
    def _falsifier_not_blank(self) -> Self:
        if not self.falsifier.strip():
            raise ValueError(f"target {self.name}: falsifier is blank")
        return self


class ModelBringupGraph(_Frozen):
    """The declared target rows. Graph shape is judged by the handler, not here."""

    targets: tuple[ModelBringupTarget, ...] = Field(min_length=1)


class ModelCheckObservation(_Frozen):
    """The result of running one target's read-only check on the host."""

    target: str = Field(pattern=_IDENTIFIER)
    state: EnumObservedState


class ModelHostBringupPlanRequest(_Frozen):
    graph: ModelBringupGraph
    observations: tuple[ModelCheckObservation, ...] = ()


class ModelPlanRefusal(_Frozen):
    kind: EnumRefusalKind
    names: tuple[str, ...]
    message: str


class ModelPlanStep(_Frozen):
    order: int = Field(ge=1)
    target: str
    stage: str
    check_kind: EnumBringupCheckKind
    observed: EnumObservedState
    decision: EnumStepDecision
    action: EnumBringupAction


class ModelHostBringupPlan(_Frozen):
    status: EnumPlanStatus
    refusals: tuple[ModelPlanRefusal, ...] = ()
    steps: tuple[ModelPlanStep, ...] = ()


__all__ = [
    "EnumBringupAction",
    "EnumBringupCheckKind",
    "EnumObservedState",
    "EnumPlanStatus",
    "EnumRefusalKind",
    "EnumStepDecision",
    "ModelBringupCheck",
    "ModelBringupGraph",
    "ModelBringupTarget",
    "ModelCheckObservation",
    "ModelHostBringupPlan",
    "ModelHostBringupPlanRequest",
    "ModelPlanRefusal",
    "ModelPlanStep",
]
