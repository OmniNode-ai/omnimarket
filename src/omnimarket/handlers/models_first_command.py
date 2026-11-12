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


def _named(model: str, url: str | None, shared: bool) -> str:
    """A model as commands name it: ``MODEL@URL`` when two local servers run it."""
    return f"{model}@{url}" if shared and url else model


def models_text(row: ModelModelStatus) -> str:
    """The models part of an ``onex models list`` line, with the one tried first.

    OMN-23413: a local model two servers run is shown once per server, as
    ``MODEL@URL``, the way ``models first``, ``remove`` and ``reorder`` name it.
    """
    names = [m.model for m in row.local_models]
    shown = (
        [
            _named(m.model, m.base_url, names.count(m.model) > 1)
            for m in row.local_models
        ]
        if row.local_models
        else list(row.models)
    )
    listed = f" [models: {', '.join(shown)}]" if shown else ""
    if not row.first_model:
        return listed
    first = _named(row.first_model, row.first_url, names.count(row.first_model) > 1)
    return listed + f" (tried first: {first})"


@click.command("first")
@click.argument(
    "provider", required=False, type=click.Choice(PROVIDERS, case_sensitive=False)
)
@click.argument("model", required=False)
@click.option(
    "--url",
    default=None,
    help="The server a local MODEL runs on; needed when two servers run it.",
)
@click.option(
    "--clear",
    is_flag=True,
    help="Forget the choice; delegation keeps its usual order again.",
)
def first_model(
    provider: str | None, model: str | None, url: str | None, clear: bool
) -> None:
    """Make MODEL of PROVIDER the model every delegation tries first.

    MODEL must be one of the models set up for PROVIDER (onex models list shows
    them); without it, PROVIDER's own first model is chosen. It moves to the
    front of PROVIDER's list and is tried before your local models and every
    other key. If it fails, the run carries on in its usual order. Nothing is
    re-run and no key is asked for. When two local servers run MODEL, --url
    says which one.
    """
    handler = model_handler()
    if clear:
        if provider is not None or url is not None:
            raise click.ClickException("--clear takes no provider, model or URL.")
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
                url=url,
            )
        )
    except ModelSetupRefusedError as refusal:
        raise click.ClickException(f"{refusal}. Nothing was changed.") from None
    chosen = model or result.models[0]
    rows = handler.handle(
        ModelModelSetupRequest(operation="status", provider=cast(ModelProvider, name))
    ).providers
    server = next(
        (row.first_url for row in rows if row.provider == name and row.first_url),
        None,
    )
    where = f" on {server}" if server else ""
    click.echo(f"Delegation now tries {MODEL_LABELS[name]} {chosen}{where} first.")


__all__: list[str] = ["first_model", "models_text"]
