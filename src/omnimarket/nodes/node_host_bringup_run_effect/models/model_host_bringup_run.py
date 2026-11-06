# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed models for the host bring-up run effect node.

The order of installer units, the installer command and the deploy-agent store file are
deployment facts: they are read from the node overlay, and the package holds no unit name,
host name or path.
"""

from __future__ import annotations

import re
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator

_UNIT = r"^[a-z][a-z0-9]*(-[a-z0-9]+)*$"
_HOST = r"^[a-z][a-z0-9-]*$"
_ENV_NAME = r"^[A-Z][A-Z0-9_]*$"


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class EnumBringupMode(StrEnum):
    """How the sequence runs. A dry run changes nothing."""

    DRY_RUN = "dry-run"
    INSTALL = "install"
    STATUS = "status"
    ROLLBACK = "rollback"


class EnumRollbackKind(StrEnum):
    """What undoes a step: its installer's uninstall, or nothing the sequence can do."""

    UNINSTALL = "uninstall"
    NONE = "none"


class EnumPreviewKind(StrEnum):
    """How a dry run reads a step: the installer's dry-run mode, or not at all.

    A unit whose installer has no dry-run mode is declared ``none``; the run then reports the
    step ``not_previewable`` and never invokes the installer, so a dry run cannot install.
    """

    DRY_RUN = "dry-run"
    NONE = "none"


class EnumStepOutcome(StrEnum):
    OK = "ok"
    FAILED = "failed"
    SKIPPED_NO_DEPLOY_STORE = "skipped_no_deploy_store"
    NOT_PREVIEWABLE = "not_previewable"
    NOT_ROLLED_BACK = "not_rolled_back"
    REFUSED_ENV = "refused_env"


class ModelBringupStepDecl(_Frozen):
    """One step of the declared order: an installer unit, its inputs and the read that proves it."""

    unit: str = Field(pattern=_UNIT)
    host_name: bool = False
    env: tuple[str, ...] = ()
    needs_deploy_store: bool = False
    rollback: EnumRollbackKind = EnumRollbackKind.NONE
    preview: EnumPreviewKind = EnumPreviewKind.DRY_RUN
    proof: str = Field(min_length=1)

    @model_validator(mode="after")
    def _env_names(self) -> ModelBringupStepDecl:
        bad = [name for name in self.env if not re.fullmatch(_ENV_NAME, name)]
        if bad:
            raise ValueError(f"env names must be upper-case identifiers: {bad}")
        return self


class ModelBringupRoleDecl(_Frozen):
    steps: tuple[ModelBringupStepDecl, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _units_unique(self) -> ModelBringupRoleDecl:
        units = [step.unit for step in self.steps]
        dupes = sorted({unit for unit in units if units.count(unit) > 1})
        if dupes:
            raise ValueError(f"a unit appears twice in one role: {dupes}")
        return self


class ModelHostBringupRunOverlay(_Frozen):
    """The node overlay: ``<root>/node_host_bringup_run_effect/overlay.yaml``."""

    installer_command: tuple[str, ...] = Field(min_length=1)
    repo_root: str = Field(min_length=1)
    deploy_store_file: str | None = None
    step_timeout_seconds: int = Field(default=600, ge=1, le=7200)
    roles: dict[str, ModelBringupRoleDecl] = Field(min_length=1)


class ModelHostBringupRunRequest(_Frozen):
    host_name: str = Field(pattern=_HOST)
    mode: EnumBringupMode = EnumBringupMode.DRY_RUN
    role: str = Field(default="linux-lab", pattern=_HOST)


class ModelInstallerOutcome(_Frozen):
    """What one installer invocation answered."""

    exit_code: int
    stdout: str = ""
    stderr: str = ""


class ModelBringupStepResult(_Frozen):
    number: int = Field(ge=1)
    unit: str
    installer_mode: str | None
    outcome: EnumStepOutcome
    exit_code: int | None = None
    detail: str = ""
    proof: str = ""


class ModelHostBringupRunResult(_Frozen):
    """The terminal event body: every step reported, and where the run stopped."""

    host_name: str
    role: str
    mode: EnumBringupMode
    steps: tuple[ModelBringupStepResult, ...]
    exit_code: int
    completed: bool
    stopped_at: int | None = None
    rollback_hint: str | None = None
    refusal: str | None = None
    changed_host: bool = False
