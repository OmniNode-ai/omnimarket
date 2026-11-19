# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""How every surface names and orders the model providers (OMN-23413).

The TUI's /config picker and the ``onex models`` CLI list the same providers
in the same order under the same names; a provider only the CLI offers keeps
its own name.
"""

from __future__ import annotations

import click

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
LISTED_PROVIDERS: tuple[ModelProvider, ...] = tuple(
    sorted(
        PROVIDERS,
        key=lambda p: (
            _PICKER_ORDER.index(p) if p in _PICKER_ORDER else len(_PICKER_ORDER)
        ),
    )
)

#: The CLI's own name for a provider the TUI's /config does not offer.
_CLI_ONLY_LABELS: dict[str, str] = {
    "anthropic": "Anthropic",
    "glm": "GLM",
    "openai_compatible": "OpenAI-compatible",  # OMN-21263: any endpoint, by URL
}
#: One name per provider on every surface (OMN-23413): the TUI's /config picker
#: names a provider, and the CLI says the same.
MODEL_LABELS: dict[str, str] = {
    **_CLI_ONLY_LABELS,
    **{p.provider: p.label for p in setup_provider_traits().picker_providers()},
}
#: The provider column's width in text output: the longest name.
LABEL_WIDTH = max(len(label) for label in MODEL_LABELS.values())


class ModelProviderChoice(click.Choice[str]):
    """The PROVIDER argument of ``onex models``: the listed providers, one name
    each, the same set and names the TUI's /config uses (OMN-23413)."""

    def __init__(self) -> None:
        super().__init__(LISTED_PROVIDERS, case_sensitive=False)


__all__: list[str] = [
    "LABEL_WIDTH",
    "LISTED_PROVIDERS",
    "MODEL_LABELS",
    "ModelProviderChoice",
]
