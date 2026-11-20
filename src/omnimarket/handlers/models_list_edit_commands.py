# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""``onex models add-model`` and ``reorder``: edit a key's chosen models (OMN-21059).

The ``onex models`` group takes these from ``LIST_EDIT_COMMANDS``; ``remove``
shares ``edit_model_list`` to say the resulting order.
"""

from __future__ import annotations

import asyncio
from typing import cast

import click

from omnimarket.handlers import handler_model_keys as model_keys
from omnimarket.handlers.model_provider_labels import MODEL_LABELS, ModelProviderChoice
from omnimarket.inference.local_byok_credential_adapter import (
    LocalByokCredentialStore,
    resolve_local_byok_credential_plan,
)
from omnimarket.nodes.node_model_setup_effect.handlers.handler_model_setup import (
    HandlerModelSetup,
    ModelSetupRefusedError,
)
from omnimarket.nodes.node_model_setup_effect.models.model_model_setup_request import (
    ModelModelSetupRequest,
    ModelProvider,
)


def edit_model_list(
    handler: HandlerModelSetup, request: ModelModelSetupRequest
) -> None:
    """Run one list edit and say the resulting order, or the refusal."""
    try:
        result = handler.handle(request)
    except ModelSetupRefusedError as refusal:
        raise click.ClickException(f"{refusal}. Nothing was changed.") from None
    label = MODEL_LABELS[cast(str, request.provider)]
    click.echo(f"Your {label} models, first tried first: {', '.join(result.models)}")


@click.command("add-model")
@click.argument("provider", type=ModelProviderChoice())
@click.option(
    "--model",
    "model_options",
    multiple=True,
    required=True,
    help="A model to add after the ones already chosen; repeat it for several.",
)
def add_model_to_key(provider: str, model_options: tuple[str, ...]) -> None:
    """Add models to PROVIDER's stored key, after the ones already chosen.

    Each model is checked against the provider's list for your key. The key is
    not asked for again.
    """
    name = provider.lower()
    if name == "local":
        raise click.ClickException(
            "add a local model with its URL, after the ones already declared: "
            f"{model_keys.LOCAL_HOW}"
        )
    store = LocalByokCredentialStore()
    key = asyncio.run(store.get_secret(f"llm.{name}.api_key"))
    if key is None:
        raise click.ClickException(
            f"no {MODEL_LABELS[name]} key is stored; add one with: "
            f"onex models add {name}"
        )
    plan = resolve_local_byok_credential_plan(name)
    for chosen in model_options:
        try:
            model_keys.check_chosen_model(name, plan, key, chosen, click.echo)
        except model_keys.ModelKeyRefusedError as refusal:
            raise click.ClickException(str(refusal)) from None
    edit_model_list(
        model_keys.model_handler(),
        ModelModelSetupRequest(
            operation="add_models",
            provider=cast(ModelProvider, name),
            models=tuple(model_options),
        ),
    )


@click.command("reorder")
@click.argument("provider", type=ModelProviderChoice())
@click.argument("models", nargs=-1, required=True)
def reorder_models(provider: str, models: tuple[str, ...]) -> None:
    """Put PROVIDER's chosen MODELS in this order, first tried first.

    Name exactly the models the key holds, each once. Nothing changes otherwise.
    A local model two servers run is named once per server, as MODEL@URL.
    """
    edit_model_list(
        model_keys.model_handler(),
        ModelModelSetupRequest(
            operation="reorder",
            provider=cast(ModelProvider, provider.lower()),
            models=models,
        ),
    )


LIST_EDIT_COMMANDS: tuple[click.Command, ...] = (add_model_to_key, reorder_models)

__all__: list[str] = ["LIST_EDIT_COMMANDS", "edit_model_list"]
