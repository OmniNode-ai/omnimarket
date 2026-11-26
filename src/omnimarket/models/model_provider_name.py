# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The model provider names shared by the setup and model-setup nodes (OMN-23413)."""

from __future__ import annotations

from typing import Annotated

from pydantic import AfterValidator

#: The models a developer chooses from, in the order onboarding lists them.
#: ``anthropic`` joined by operator ruling 2026-10-11T15:05:50Z (OMN-20990).
#: ``glm`` (OMN-20157): z.ai's general API, the catalogue's customer GLM plan.
#: ``local`` (OMN-21162): a model server on an OpenAI-compatible URL, no key.
DECLARED_PROVIDERS: tuple[str, ...] = (
    "gemini",
    "openrouter",
    "openai",
    "reflection",  # OMN-23413: Reflection AI's OpenAI-compatible API, by key
    "anthropic",
    "glm",
    "hopscotch",  # OMN-26688: Hopscotch's OpenAI-compatible router, by key
    "local",
    # OMN-21263: any other endpoint that speaks the OpenAI API, by URL, with a key.
    "openai_compatible",
)


def _listed() -> frozenset[str]:
    from omnimarket.handlers.routing.byok_catalog_providers import (
        load_byok_catalog_providers,
    )

    return frozenset(load_byok_catalog_providers())


def known_provider(value: str) -> str:
    """``value`` when onex offers it: declared here, or on the shipped models.dev
    list under its own id (OMN-26688). Anything else is refused, as the Literal
    this replaced refused it."""
    if value in DECLARED_PROVIDERS or value in _listed():
        return value
    raise ValueError(
        f"{value!r} is not a provider onex offers: one of "
        f"{', '.join(DECLARED_PROVIDERS)}, or a provider id on the models.dev list"
    )


def all_providers() -> tuple[str, ...]:
    """The declared providers in their order, then the models.dev list's, A-Z by id."""
    return (
        *DECLARED_PROVIDERS,
        *sorted(p for p in _listed() if p not in DECLARED_PROVIDERS),
    )


#: A provider name: one onex offers, checked by :func:`known_provider`.
ModelProvider = Annotated[str, AfterValidator(known_provider)]
