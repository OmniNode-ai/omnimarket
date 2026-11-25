# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Explicit endpoint discovery, shared by model management and onboarding."""

import json
import sys
from getpass import getpass

import click
from pydantic import SecretStr, ValidationError

from omnimarket.handlers import model_catalog
from omnimarket.models.model_endpoint_models import (
    ModelEndpointModelsRequest,
)
from omnimarket.nodes.node_model_setup_effect.handlers.handler_endpoint_models import (
    HandlerEndpointModels,
)


@click.command("discover")
@click.option(
    "--url", required=True, help="Base URL of your already-running model endpoint."
)
@click.option(
    "--api-key",
    is_flag=True,
    help="Read this endpoint's key from hidden input or stdin.",
)
@click.option("--json", "as_json", is_flag=True)
def discover_command(url: str, api_key: bool, as_json: bool) -> None:
    """List every model exposed by an endpoint; do not run or download models."""
    key = None
    if api_key:
        value = (
            getpass("Endpoint API key: ") if sys.stdin.isatty() else sys.stdin.read()
        )
        if not value.strip():
            raise click.ClickException(
                "An endpoint key is required on hidden input or stdin."
            )
        key = SecretStr(value.strip())
    try:
        request = ModelEndpointModelsRequest(url=url, key=key)
    except ValidationError:
        raise click.ClickException(
            "Invalid endpoint URL. Use HTTP(S) without embedded credentials, query or fragment."
        ) from None
    result = HandlerEndpointModels().handle(request)
    if as_json:
        click.echo(result.model_dump_json())
    elif result.status == "ready":
        for model in result.models:
            click.echo(model)
        if not result.models:
            click.echo("The endpoint exposes no models.")
    else:
        click.echo(result.message, err=True)
    if result.status != "ready":
        raise click.exceptions.Exit(1)


@click.command("refresh-prices")
@click.option("--json", "as_json", is_flag=True)
def refresh_prices_command(as_json: bool) -> None:
    """Read the model price list from models.dev and keep it for delegation calls.

    A delegation call never reads models.dev: it prices from the copy this
    command (and onex setup) keeps, else from the price list shipped in the
    install. A failed read leaves the earlier copy in place.
    """
    index = model_catalog.refresh_models_dev()
    if as_json:
        click.echo(
            json.dumps(
                {
                    "models": len(index.models),
                    "read_at": index.fetched_at,
                    "error": index.error,
                }
            )
        )
    elif index.error is None:
        click.echo(f"Kept {len(index.models)} model prices read at {index.fetched_at}.")
    else:
        click.echo(
            f"{index.error}; delegation calls keep pricing from the earlier copy"
            " or the price list shipped with this install.",
            err=True,
        )
    if index.error is not None:
        raise click.exceptions.Exit(1)
