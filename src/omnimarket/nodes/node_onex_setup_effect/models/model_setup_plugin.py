# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Versioned package declarations for setup capabilities and their existing effects."""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue, field_validator

PluginCallable = Annotated[
    str, Field(pattern=r"^[a-zA-Z_][a-zA-Z0-9_.]*:[a-zA-Z_][a-zA-Z0-9_]*$")
]


class ModelSetupPluginLifecycle(BaseModel):
    """Real activation/disposal entry points, each run in a bounded child."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    enable: PluginCallable | None = None
    disable: PluginCallable | None = None
    timeout_seconds: int = Field(default=30, ge=1, le=300)
    failure_policy: Literal["refuse"] = "refuse"


class ModelSetupPluginStep(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(pattern=r"^[a-z][a-z0-9_-]*$")
    stage: Literal["setup", "completion"] = "setup"
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
    recommended: bool = False
    consent_notice: str | None = Field(default=None, max_length=1000)
    enabled_by_default: bool = True
    commands: dict[str, PluginCallable] = Field(default_factory=dict)
    configuration_schema: PluginCallable | None = None
    lifecycle: ModelSetupPluginLifecycle = Field(
        default_factory=ModelSetupPluginLifecycle
    )
    steps: tuple[ModelSetupPluginStep, ...] = Field(min_length=1)

    @field_validator("components", "requires")
    @classmethod
    def unique_names(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(set(value)) != len(value) or any(not name.strip() for name in value):
            raise ValueError(
                "Plugin selections and dependencies must be unique nonempty names"
            )
        return value

    @field_validator("commands")
    @classmethod
    def valid_commands(cls, value: dict[str, str]) -> dict[str, str]:
        import re

        if any(re.fullmatch(r"[a-z][a-z0-9-]*", name) is None for name in value):
            raise ValueError("Plugin commands must be lowercase command names")
        return value


class ModelSetupPluginResolution(BaseModel):
    """Validated capability order without constructing or executing effects."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    plugins: tuple[ModelSetupPlugin, ...]


class ModelSetupPluginPreferences(BaseModel):
    """The plugin section of the existing user configuration."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)
    enabled: dict[str, bool] = Field(default_factory=dict)
    configuration: dict[str, dict[str, JsonValue]] = Field(default_factory=dict)


class ModelSetupPluginAction(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    plugin: str = Field(pattern=r"^[a-z][a-z0-9_-]*$")
    action: Literal["enable", "disable"]


class ModelSetupPluginActionResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    plugin: str
    enabled: bool
    changed: bool
