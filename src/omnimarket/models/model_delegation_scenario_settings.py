# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The settings overlay of a delegation readiness scenario (OMN-23602).

A scenario's keys and addresses already live under names someone created: in the host's environment file
and in the secret store. The overlay names them, so no one sets a new variable by hand. Each setting fills one
variable of the run's environment from exactly one of: an existing secret ``secret`` (looked up in the
environment, then the host environment file, then the store folder), a ``template`` whose ``{NAME}`` parts are
existing names looked up the same way, or a literal non-secret ``value``. The overlay holds names and literals
only, never a key.
"""

from __future__ import annotations

import re
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from omnimarket.models.secret_store.model_secret_store_overlay import (
    ModelSecretStoreOverlay,
)

NAME_PATTERN = r"^[A-Za-z_][A-Za-z0-9_]*$"
_TEMPLATE_PART = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")


class ModelScenarioSetting(BaseModel):
    """One run variable and the one source that fills it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    env: str = Field(pattern=NAME_PATTERN)
    secret: str | None = Field(default=None, pattern=NAME_PATTERN)
    template: str | None = Field(default=None, min_length=1)
    value: str | None = Field(default=None, min_length=1)

    @model_validator(mode="after")
    def _exactly_one_source(self) -> ModelScenarioSetting:
        sources = [s for s in (self.secret, self.template, self.value) if s is not None]
        if len(sources) != 1:
            raise ValueError(
                f"setting {self.env}: exactly one of secret, template or value is required"
            )
        if self.template is not None and not _TEMPLATE_PART.search(self.template):
            raise ValueError(
                f"setting {self.env}: a template names at least one {{NAME}}"
            )
        return self

    def source_names(self) -> tuple[str, ...]:
        """The existing names this setting reads, in the order they are needed."""
        if self.secret is not None:
            return (self.secret,)
        if self.template is not None:
            return tuple(dict.fromkeys(_TEMPLATE_PART.findall(self.template)))
        return ()

    def render(self, found: dict[str, str]) -> str | None:
        """The setting's value from the found names, or ``None`` when a name it needs was not found."""
        if self.value is not None:
            return self.value
        names = self.source_names()
        if any(not found.get(name, "").strip() for name in names):
            return None
        if self.secret is not None:
            return found[self.secret]
        assert self.template is not None
        return _TEMPLATE_PART.sub(lambda m: found[m.group(1)], self.template)


class ModelScenarioSettingsOverlay(BaseModel):
    """The overlay file of one scenario node: where its settings are found."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    #: The store folder of the house's environment secrets.
    folder: str = Field(min_length=1)
    #: The host environment file, relative to the home, searched after the process environment.
    host_env_file: str = Field(min_length=1)
    settings: tuple[ModelScenarioSetting, ...] = Field(min_length=1)


class ModelScenarioTenantReadOverlay(BaseModel):
    """The bus lane of a scenario's tenant reads: the bus and lane to open, the probe tenant and the wait bound."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    bus: Literal["kafka", "inmemory"]
    bus_lane: str = Field(min_length=1)
    probe_tenant_id: UUID
    timeout_seconds: float = Field(gt=0)


class ModelScenarioStoreOverlay(BaseModel):
    """The overlay document of a scenario node that names its store: names and addresses only, never a value."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    secret_store: ModelSecretStoreOverlay
    tenant_read: ModelScenarioTenantReadOverlay | None = None


__all__ = [
    "NAME_PATTERN",
    "ModelScenarioSetting",
    "ModelScenarioSettingsOverlay",
    "ModelScenarioStoreOverlay",
    "ModelScenarioTenantReadOverlay",
]
