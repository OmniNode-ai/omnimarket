# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Explicit endpoint discovery, shared by model management and onboarding."""

import sys
from getpass import getpass

import click
from pydantic import SecretStr, ValidationError

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
