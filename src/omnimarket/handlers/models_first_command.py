# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""``onex models first``: choose the model every delegation tries first (OMN-21346)."""

from __future__ import annotations

from typing import cast

import click

from omnimarket.handlers.handler_model_keys import MODEL_LABELS, model_handler
from omnimarket.nodes.node_model_setup_effect.handlers.handler_model_setup import (
    PROVIDERS,
    ModelSetupRefusedError,
)
from omnimarket.nodes.node_model_setup_effect.models.model_model_setup_request import (
    ModelModelSetupRequest,
    ModelProvider,
)
from omnimarket.nodes.node_model_setup_effect.models.model_model_setup_result import (
    ModelModelStatus,
)


def models_text(row: ModelModelStatus) -> str:
    """The models part of an ``onex models list`` line, with the one tried first."""
    listed = f" [models: {', '.join(row.models)}]" if row.models else ""
    return listed + (f" (tried first: {row.first_model})" if row.first_model else "")


@click.command("first")
@click.argument(
    "provider", required=False, type=click.Choice(PROVIDERS, case_sensitive=False)
)
@click.argument("model", required=False)
@click.option(
    "--clear",
    is_flag=True,
    help="Forget the choice; delegation keeps its usual order again.",
)
def first_model(provider: str | None, model: str | None, clear: bool) -> None:
    """Make MODEL of PROVIDER the model every delegation tries first.

    MODEL must be one of the models set up for PROVIDER (onex models list shows
    them); without it, PROVIDER's own first model is chosen. It moves to the
    front of PROVIDER's list and is tried before your local models and every
    other key. If it fails, the run carries on in its usual order. Nothing is
    re-run and no key is asked for.
    """
    handler = model_handler()
    if clear:
        if provider is not None:
            raise click.ClickException("--clear takes no provider or model.")
        handler.handle(ModelModelSetupRequest(operation="clear_first"))
        click.echo(
            "No model is chosen to be tried first; delegation keeps its usual order."
        )
        return
    if provider is None:
        raise click.ClickException(
            "name the provider, and optionally the model: onex models first "
            "<provider> [<model>]. onex models list shows what is set up."
        )
    name = provider.lower()
    try:
        result = handler.handle(
            ModelModelSetupRequest(
                operation="set_first",
                provider=cast(ModelProvider, name),
                models=(model,) if model else (),
            )
        )
    except ModelSetupRefusedError as refusal:
        raise click.ClickException(f"{refusal}. Nothing was changed.") from None
    chosen = model or result.models[0]
    click.echo(f"Delegation now tries {MODEL_LABELS[name]} {chosen} first.")


__all__: list[str] = ["first_model", "models_text"]
