# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Versioned package declarations for setup capabilities and their existing effects."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue, field_validator


class ModelSetupPluginStep(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(pattern=r"^[a-z][a-z0-9_-]*$")
    factory: str = Field(pattern=r"^[a-zA-Z_][a-zA-Z0-9_.]*:[a-zA-Z_][a-zA-Z0-9_]*$")
    options: dict[str, JsonValue] = Field(default_factory=dict)
    context: tuple[Literal["onex_home", "credentials", "model_step"], ...] = ()


class ModelSetupPlugin(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str = Field(pattern=r"^[a-z][a-z0-9_-]*$")
    api_version: Literal[1]
    version: str = Field(pattern=r"^\d+\.\d+\.\d+$")
    components: tuple[str, ...] = ()
    requires: tuple[str, ...] = ()
    automatic: bool = False
    steps: tuple[ModelSetupPluginStep, ...] = Field(min_length=1)

    @field_validator("components", "requires")
    @classmethod
    def unique_names(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(set(value)) != len(value) or any(not name.strip() for name in value):
            raise ValueError(
                "Plugin selections and dependencies must be unique nonempty names"
            )
        return value
