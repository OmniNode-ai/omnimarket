# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Setup inputs per provider, read from setup_provider_traits.yaml (OMN-23413)."""

from __future__ import annotations

from functools import cache
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict


class ModelSetupPickerProvider(BaseModel):
    """One provider the TUI's /config offers."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    provider: str
    label: str
    hint: str = ""
    default_port: int | None = None
    selected: bool = False


class ModelSetupProviderTraits(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    url_required: frozenset[str]
    endpoint_auth: frozenset[str]
    key_provider: dict[str, str]
    registers_as: dict[str, dict[str, str]]
    picker: tuple[ModelSetupPickerProvider, ...] = ()

    def key_name(self, provider: str) -> str:
        """The credential owner a provider's key is stored under."""
        return self.key_provider.get(provider, provider)

    def needs_url(self, provider: str) -> bool:
        """A server the user runs (asked for its URL), not a hosted key."""
        return provider in self.url_required

    def picker_providers(self) -> tuple[ModelSetupPickerProvider, ...]:
        """The /config choices, sorted by label."""
        return tuple(sorted(self.picker, key=lambda entry: entry.label.lower()))

    def registration(self, provider: str, *, keyed: bool) -> str:
        """The canonical endpoint owner a selection registers as."""
        return self.registers_as.get(provider, {}).get(
            "keyed" if keyed else "keyless", provider
        )


@cache
def setup_provider_traits() -> ModelSetupProviderTraits:
    return ModelSetupProviderTraits.model_validate(
        yaml.safe_load(
            (
                Path(__file__).resolve().parents[1] / "setup_provider_traits.yaml"
            ).read_text()
        )
    )
