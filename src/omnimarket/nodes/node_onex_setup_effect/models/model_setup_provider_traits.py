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
        """The /config choices: setup_provider_traits.yaml's in its order, then each
        provider on the shipped models.dev list (OMN-26688)."""
        declared = {p.provider for p in self.picker}
        return (
            *self.picker,
            *(p for p in _listed_picker() if p.provider not in declared),
        )

    def registration(self, provider: str, *, keyed: bool) -> str:
        """The canonical endpoint owner a selection registers as."""
        return self.registers_as.get(provider, {}).get(
            "keyed" if keyed else "keyless", provider
        )


@cache
def _listed_picker() -> tuple[ModelSetupPickerProvider, ...]:
    from omnimarket.handlers.routing.byok_catalog_providers import (
        load_byok_catalog_providers,
    )

    return tuple(
        ModelSetupPickerProvider(
            provider=listed.id,
            label=listed.name,
            hint=(
                f"Create a key with {listed.name}; its docs are at {listed.doc}."
                if listed.doc
                else f"Create a key with {listed.name}."
            ),
        )
        for listed in load_byok_catalog_providers().values()
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
