# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One source the installer scan found (OMN-20804)."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from .enum_automation_liveness_source_class import EnumAutomationLivenessSourceClass
from .model_automation_liveness_unit_reference import (
    ModelAutomationLivenessUnitReference,
)


class ModelAutomationLivenessFoundSource(BaseModel):
    """A unit file, plist, cron file or installer, with the unit names its file name implies."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    repository: str
    path: str
    source_class: EnumAutomationLivenessSourceClass
    #: Native ids the file name alone implies; empty for installer code, whose names are declared.
    implied_native_ids: tuple[str, ...] = ()
    #: Units a manifest names; every one needs an entry, unlike ``implied_native_ids``, of which
    #: any one is enough.
    required_units: tuple[ModelAutomationLivenessUnitReference, ...] = ()
