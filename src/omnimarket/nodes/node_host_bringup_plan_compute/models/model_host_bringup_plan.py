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
_TARGET_NAME = r"^[a-z][a-z0-9]*([_-][a-z0-9]+)*$"
_STAGE = r"^[A-Z]$"


class EnumBringupCheckKind(StrEnum):
    """The read-only already-satisfied probe a target row declares.

    The set is closed and covers the 57 targets of the host bring-up graph: each
    value names a family of probe, and the symbolic ``subject`` of the row picks
    the one the host profile resolves.
    """

    DECLARATION_VALID = "declaration_valid"
    DECLARATION_DRIFT = "declaration_drift"
    REVISION_CURRENT = "revision_current"
    PERSON_CONFIRMED = "person_confirmed"
    MANAGEMENT_READ = "management_read"
    TRANSPORT_ANSWERS = "transport_answers"
    HOST_STATE = "host_state"
    FILE_STATE = "file_state"
    PACKAGE_INSTALLED = "package_installed"
    SERVICE_ACTIVE = "service_active"
    GROUP_MEMBER = "group_member"
    UNIT_STATUS = "unit_status"
    CHECKOUT_STATE = "checkout_state"
    CREDENTIAL_RESOLVES = "credential_resolves"
    MEASUREMENT_CURRENT = "measurement_current"
    TABLE_ROW = "table_row"
    RUNBOOK_VERIFIED = "runbook_verified"
    FLEET_HEALTHY = "fleet_healthy"
    AGGREGATE = "aggregate"


class EnumBringupAction(StrEnum):
    """What the bring-up does when a target is not already satisfied.

    The eight host primitives of the plan (``FILE_CONTENT`` to
    ``GUARDED_SSHD_DROPIN``) and ``HOST_UNIT_INSTALL`` are the section 4.1 kinds;
    the rest name the other ways a target is met: a person step, a stop, a data
    pull request, or a call to a broker, the store or a repository.
    """

    PERSON_STEP = "person_step"
    STOP = "stop"
    NO_ACTION = "no_action"
    LOCAL_INCLUDE = "local_include"
    DATA_PR = "data_pr"
    ADOPT_MERGED = "adopt_merged"
    MANAGEMENT_WRITE = "management_write"
    FILE_CONTENT = "file_content"
    DIRECTORY = "directory"
    APT_SOURCE = "apt_source"
    APT_PACKAGES = "apt_packages"
    GROUP_MEMBER = "group_member"
    SERVICE_ENABLED = "service_enabled"
    LINE_IN_FILE = "line_in_file"
    GUARDED_SSHD_DROPIN = "guarded_sshd_dropin"
    HOST_UNIT_INSTALL = "host_unit_install"
    TOOL_INSTALL = "tool_install"
    CLONE_REPOSITORY = "clone_repository"
    SYNC_ENVIRONMENT = "sync_environment"
    INSTALL_PLUGIN_TREE = "install_plugin_tree"
    TAILNET_LOGIN = "tailnet_login"
    STORE_CREDENTIAL = "store_credential"
    MINT_LOGIN = "mint_login"
    APPLY_BROKER_GRANTS = "apply_broker_grants"
    REFILL_CLAUDE_POOL = "refill_claude_pool"
    ROLE_ACTION = "role_action"
    RUN_WORKLOAD = "run_workload"
    PREPARE_VOLUME = "prepare_volume"
    MOVE_DATA_ROOTS = "move_data_roots"
    RERUN_TARGETS = "rerun_targets"
    DEPLOY_RUNNERS = "deploy_runners"


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

    name: str = Field(pattern=_TARGET_NAME)
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

    target: str = Field(pattern=_TARGET_NAME)
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
