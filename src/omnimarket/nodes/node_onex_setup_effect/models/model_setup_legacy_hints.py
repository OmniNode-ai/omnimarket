# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Unverified selections imported from the previous shell onboarding walk."""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, field_validator

from omnimarket.nodes.node_onex_setup_effect.models.model_setup_provider import (
    ModelSetupProvider,
)


class ModelSetupLegacyHints(BaseModel):
    """Selection hints for review; completion marks carry no effect evidence."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    providers: tuple[
        Literal[
            "gemini", "openrouter", "openai", "ollama", "local", "openai_compatible"
        ],
        ...,
    ] = ()
    containers: bool | None = None
    local_url: str | None = None
    local_model: str | None = None
    completed_phases: tuple[int, ...] = ()
    infra_anchor: str | None = None

    @field_validator("local_url")
    @classmethod
    def _url(cls, value: str | None) -> str | None:
        # Reuse the setup provider's URL policy inside this node.
        return ModelSetupProvider.validate_endpoint(value)

    @field_validator("local_model")
    @classmethod
    def _model(cls, value: str | None) -> str | None:
        if value is not None and (not value.strip() or "\x00" in value):
            raise ValueError("Invalid legacy model hint")
        return value

    @field_validator("providers")
    @classmethod
    def _providers(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(set(value)) != len(value):
            raise ValueError("Invalid legacy provider hints")
        return value

    @field_validator("completed_phases")
    @classmethod
    def _phases(cls, value: tuple[int, ...]) -> tuple[int, ...]:
        if len(set(value)) != len(value) or any(p not in range(1, 7) for p in value):
            raise ValueError("Invalid legacy phase hints")
        return value

    @field_validator("infra_anchor")
    @classmethod
    def _anchor(cls, value: str | None) -> str | None:
        if value is not None and not re.fullmatch(r"[a-f0-9]{40}", value):
            raise ValueError("Invalid legacy anchor hint")
        return value
