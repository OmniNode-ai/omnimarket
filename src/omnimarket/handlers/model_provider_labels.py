# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""How every surface names and orders the model providers (OMN-23413).

The TUI's /config picker and the ``onex models`` CLI list the same providers
in the same order under the same names; a provider only the CLI offers keeps
its own name.
"""

from __future__ import annotations

import click

from omnimarket.models.model_provider_name import all_providers
from omnimarket.nodes.node_model_setup_effect.handlers.handler_model_setup import (
    PROVIDERS,
)
from omnimarket.nodes.node_model_setup_effect.models.model_model_setup_request import (
    ModelProvider,
)
from omnimarket.nodes.node_onex_setup_effect.models.model_setup_provider_traits import (
    setup_provider_traits,
)

#: OMN-23413: the order every surface lists providers in: the TUI's /config
#: picker first, then the ones only the CLI offers. Every model server the
#: developer runs is one Local Model, named by its endpoint URL.
_PICKER_ORDER = [p.provider for p in setup_provider_traits().picker_providers()]
_DECLARED_ORDER: tuple[ModelProvider, ...] = tuple(
    sorted(
        PROVIDERS,
        key=lambda p: (
            _PICKER_ORDER.index(p) if p in _PICKER_ORDER else len(_PICKER_ORDER)
        ),
    )
)
#: OMN-26688: then each provider on the shipped models.dev list, A-Z by id.
LISTED_PROVIDERS: tuple[ModelProvider, ...] = (
    *_DECLARED_ORDER,
    *(p for p in all_providers() if p not in PROVIDERS),
)

#: The CLI's own name for a provider the TUI's /config does not offer.
_CLI_ONLY_LABELS: dict[str, str] = {
    "anthropic": "Anthropic",
    # docs.z.ai (read 2026-11-26): the provider is Z.AI; GLM is its model family.
    "glm": "Z.AI",
    "openai_compatible": "OpenAI-compatible",  # OMN-21263: any endpoint, by URL
}
#: One name per provider on every surface (OMN-23413): the TUI's /config picker
#: names a provider, and the CLI says the same.
MODEL_LABELS: dict[str, str] = {
    **_CLI_ONLY_LABELS,
    **{p.provider: p.label for p in setup_provider_traits().picker_providers()},
}
#: The provider column's width in text output: the longest name.
#: The declared providers' names set it; a longer models.dev name runs past it.
LABEL_WIDTH = max(len(MODEL_LABELS[p]) for p in PROVIDERS)


#: OMN-26688, operator 2026-11-26: the name a user types for a provider whose id
#: differs. Z.AI's keys stay stored under ``glm``, so a stored key keeps routing.
_ALIASES: dict[str, str] = {"zai": "glm"}
_SHOWN: dict[str, str] = {provider: alias for alias, provider in _ALIASES.items()}
#: Every name a user can type for a provider: the listed ids and the aliases.
TYPED_PROVIDERS: frozenset[str] = frozenset((*LISTED_PROVIDERS, *_ALIASES))
#: The declared providers as a user types them, in the order every surface uses.
SHOWN_DECLARED: tuple[str, ...] = tuple(_SHOWN.get(p, p) for p in _DECLARED_ORDER)


class ModelProviderChoice(click.Choice[str]):
    """The PROVIDER argument of ``onex models``: the listed providers, one name
    each, the same set and names the TUI's /config uses (OMN-23413)."""

    def __init__(self) -> None:
        super().__init__(LISTED_PROVIDERS, case_sensitive=False)

    def convert(
        self, value: object, param: click.Parameter | None, ctx: click.Context | None
    ) -> str:
        """``zai`` names Z.AI, whose keys are stored under the id ``glm``."""
        if isinstance(value, str):
            value = _ALIASES.get(value.strip().lower(), value)
        return str(super().convert(value, param, ctx))

    def get_metavar(self, param: click.Parameter, ctx: click.Context) -> str:
        """PROVIDER: the names, one token too long for an 80-column usage line,
        are in the command's help instead (verify pass 2026-11-26)."""
        return "PROVIDER"

    def get_invalid_choice_message(
        self, value: object, ctx: click.Context | None
    ) -> str:
        return (
            f"{value!r} is not a provider onex offers. Use one of "
            f"{', '.join(SHOWN_DECLARED)}, or a provider "
            "id from the models.dev "
            "list, such as deepseek."
        )


__all__: list[str] = [
    "LABEL_WIDTH",
    "LISTED_PROVIDERS",
    "MODEL_LABELS",
    "SHOWN_DECLARED",
    "TYPED_PROVIDERS",
    "ModelProviderChoice",
]
