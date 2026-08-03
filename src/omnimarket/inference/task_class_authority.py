# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed access to the Market-owned task-class and Gateway exposure authority."""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

_DEFAULT_AUTHORITY_PATH = (
    Path(__file__).resolve().parent.parent / "configs" / "task_class_contracts.v1.yaml"
)


class EnumGatewayExposure(StrEnum):
    """Closed Gateway exposure policy for a Market task class."""

    PUBLIC = "public"
    INTERNAL = "internal"


class ModelTaskClassAuthorityEntry(BaseModel):
    """Authority fields shared by every task-class routing contract entry."""

    model_config = ConfigDict(frozen=True, extra="allow")

    gateway_exposure: EnumGatewayExposure


class ModelTaskClassAuthority(BaseModel):
    """Market-owned task-class universe with a total Gateway exposure partition."""

    model_config = ConfigDict(frozen=True, extra="allow")

    task_classes: dict[str, ModelTaskClassAuthorityEntry] = Field(min_length=1)

    @field_validator("task_classes")
    @classmethod
    def _validate_task_class_names(
        cls,
        value: dict[str, ModelTaskClassAuthorityEntry],
    ) -> dict[str, ModelTaskClassAuthorityEntry]:
        invalid = sorted(name for name in value if not name or name != name.strip())
        if invalid:
            raise ValueError(
                f"task class names must be non-empty and trimmed: {invalid}"
            )
        return value

    @property
    def universe(self) -> frozenset[str]:
        """Return the registry keys, which are the sole task-class denominator."""
        return frozenset(self.task_classes)

    @property
    def public_task_classes(self) -> frozenset[str]:
        """Return the exact public Gateway projection."""
        return self._classes_with_exposure(EnumGatewayExposure.PUBLIC)

    @property
    def internal_task_classes(self) -> frozenset[str]:
        """Return valid Market classes excluded from the public Gateway."""
        return self._classes_with_exposure(EnumGatewayExposure.INTERNAL)

    def _classes_with_exposure(
        self,
        exposure: EnumGatewayExposure,
    ) -> frozenset[str]:
        return frozenset(
            name
            for name, entry in self.task_classes.items()
            if entry.gateway_exposure is exposure
        )


def load_task_class_authority(
    config_path: str | Path | None = None,
) -> ModelTaskClassAuthority:
    """Load the Market authority and fail closed on missing/unknown exposure."""
    path = Path(config_path) if config_path is not None else _DEFAULT_AUTHORITY_PATH
    if not path.is_file():
        raise FileNotFoundError(f"task-class authority not found at {path}")
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"task-class authority root must be a mapping: {path}")
    try:
        return ModelTaskClassAuthority.model_validate(raw)
    except ValidationError as exc:
        msg = f"task-class authority validation failed for {path}: {exc}"
        raise ValueError(msg) from exc


__all__ = [
    "EnumGatewayExposure",
    "ModelTaskClassAuthority",
    "ModelTaskClassAuthorityEntry",
    "load_task_class_authority",
]
