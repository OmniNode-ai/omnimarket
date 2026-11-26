# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The settings of the delegation check regression run.

Every threshold, owner, alarm target rule and key-to-check rule is a setting; none has a model default, so a missing
one fails the load. ``overlay.defaults.yaml`` beside this file is the shipped default set the tests load; a deployment's
own values arrive through the settings store and replace it.
"""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from omnimarket.nodes.node_delegation_check_regression_effect.models.model_check_regression import (
    EnumChangeKind,
)

DEFAULTS_PATH = Path(__file__).resolve().parent / "overlay.defaults.yaml"

#: A target no alarm may name. The overlay may add to refused_targets, never drop this one.
PROD_TARGET = "prod"


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class ModelKeyCheckRule(_Frozen):
    """A change that selects checks. The globs match the change's subject and each changed key (a slash-joined setting path)."""

    change_kind: EnumChangeKind
    subject_glob: str = Field(min_length=1)
    key_glob: str = Field(min_length=1)
    checks: tuple[str, ...] = Field(min_length=1)


class ModelCheckSetting(_Frozen):
    #: The named owner an alarm for this check goes to.
    owner: str = Field(min_length=1)
    #: How long the run waits for the check's reading after the change.
    read_deadline_seconds: int = Field(gt=0)
    #: Consecutive red readings after a change before the alarm; 1 alarms on the first.
    red_streak_to_alarm: int = Field(ge=1)


class ModelCheckRegressionSettings(_Frozen):
    #: The alarm condition the delivery routes by.
    condition: str = Field(min_length=1)
    #: A red check alarms within this many seconds of the change.
    alarm_deadline_seconds: int = Field(gt=0)
    refused_targets: tuple[str, ...]
    #: The lane a settings-change trigger runs the checks against; a refused target (prod) is never run.
    trigger_target: str = Field(min_length=1)
    key_check_map: tuple[ModelKeyCheckRule, ...] = Field(min_length=1)
    checks: dict[str, ModelCheckSetting] = Field(min_length=1)

    @model_validator(mode="after")
    def _closed(self) -> ModelCheckRegressionSettings:
        if PROD_TARGET not in {t.lower() for t in self.refused_targets}:
            raise ValueError(f"refused_targets must refuse {PROD_TARGET!r}")
        unknown = sorted(
            {c for r in self.key_check_map for c in r.checks} - set(self.checks)
        )
        if unknown:
            raise ValueError(f"key_check_map names checks with no setting: {unknown}")
        for check_id, check in self.checks.items():
            if check.read_deadline_seconds > self.alarm_deadline_seconds:
                raise ValueError(
                    f"check {check_id}: read_deadline_seconds exceeds alarm_deadline_seconds"
                )
        return self


def load_settings(path: Path = DEFAULTS_PATH) -> ModelCheckRegressionSettings:
    return ModelCheckRegressionSettings.model_validate(
        yaml.safe_load(path.read_text(encoding="utf-8"))
    )


__all__ = [
    "DEFAULTS_PATH",
    "PROD_TARGET",
    "ModelCheckRegressionSettings",
    "ModelCheckSetting",
    "ModelKeyCheckRule",
    "load_settings",
]
