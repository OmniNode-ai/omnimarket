# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Named model connections on the existing onex.models command extension."""

import sys
from getpass import getpass

import click
from pydantic import SecretStr, ValidationError

from omnimarket.nodes.node_model_setup_effect.handlers.handler_endpoint_connections import (
    HandlerEndpointConnections,
)
from omnimarket.nodes.node_model_setup_effect.models.model_endpoint_connection import (
    ModelEndpointConnectionRequest,
    ModelEndpointConnectionResult,
)


def _emit(result: ModelEndpointConnectionResult, as_json: bool) -> None:
    if as_json:
        click.echo(result.model_dump_json())
    else:
        for connection in result.connections:
            click.echo(f"{connection.connection_id}: {connection.url}")
            for model, backend in zip(
                connection.models, connection.backend_ids, strict=True
            ):
                click.echo(f"  {model} ({backend})")
        for model in result.catalog:
            click.echo(model)
        for test in result.tests:
            click.echo(
                f"[{test.status}] {test.model}; backend={test.backend_id}; tested_at={test.tested_at}"
            )
        if result.message:
            click.echo(result.message, err=result.status != "ready")
    if result.status != "ready":
        raise click.exceptions.Exit(1)


def _request(**values: object) -> ModelEndpointConnectionRequest:
    try:
        return ModelEndpointConnectionRequest.model_validate(values)
    except ValidationError:
        raise click.ClickException(
            "Invalid connection selections. Use a simple connection name, HTTP(S) endpoint without embedded secrets, and unique model IDs."
        ) from None


@click.command("connect")
@click.argument("name")
@click.option("--url", required=True, help="Your already-running model endpoint.")
@click.option("--model", "models", multiple=True, help="Repeat in preference order.")
@click.option(
    "--api-key", is_flag=True, help="Read this server's key from hidden input or stdin."
)
@click.option(
    "--native-messages/--no-native-messages",
    default=None,
    help="Declare this server also supports Anthropic Messages; compatibility requires a separate Claude proof.",
)
@click.option(
    "--test/--no-test",
    "prove",
    default=True,
    help="Run a pinned delegation for each selected model.",
)
@click.option(
    "--yes",
    is_flag=True,
    help="Accept configured model inference and any provider charges.",
)
@click.option("--json", "as_json", is_flag=True)
def connect_command(
    name: str,
    url: str,
    models: tuple[str, ...],
    api_key: bool,
    native_messages: bool | None,
    prove: bool,
    yes: bool,
    as_json: bool,
) -> None:
    """Connect a named server without installing or running model software."""
    if prove and not yes:
        if as_json or not sys.stdin.isatty():
            raise click.ClickException(
                "Review the selected endpoint's inference cost, then repeat with --yes; --no-test configures without claiming inference proof."
            )
        if not click.confirm(
            "Run model tests, accepting any provider API charges?", default=False
        ):
            raise click.Abort()
    key = None
    if api_key:
        value = (
            getpass("Endpoint API key: ") if sys.stdin.isatty() else sys.stdin.read()
        )
        if not value.strip():
            raise click.ClickException(
                "Supply the endpoint key on hidden input or stdin."
            )
        key = SecretStr(value.strip())
    handler = HandlerEndpointConnections()
    request = _request(
        operation="connect",
        connection_id=name,
        url=url,
        models=models,
        key=key,
        prove=prove,
        native_messages=native_messages,
    )
    result = handler.handle(request)
    if not models and result.catalog and not as_json and sys.stdin.isatty():
        for model in result.catalog:
            click.echo(model)
        picked = tuple(
            m.strip()
            for m in click.prompt(
                "Models in preference order (comma-separated IDs)"
            ).split(",")
        )
        request = _request(**{**request.model_dump(), "key": key, "models": picked})
        result = handler.handle(request)
    _emit(result, as_json)


@click.command("connections")
@click.option("--json", "as_json", is_flag=True)
def connections_command(as_json: bool) -> None:
    """List named endpoint configuration and recorded proofs without mutation."""
    _emit(HandlerEndpointConnections().handle(_request(operation="list")), as_json)


@click.command("refresh-connection")
@click.argument("name")
@click.option("--json", "as_json", is_flag=True)
def refresh_connection_command(name: str, as_json: bool) -> None:
    """Read this connection's current catalog with its own credential."""
    _emit(
        HandlerEndpointConnections().handle(
            _request(operation="refresh", connection_id=name)
        ),
        as_json,
    )


@click.command("select-connection")
@click.argument("name")
@click.argument("model", required=False)
@click.option("--json", "as_json", is_flag=True)
def select_connection_command(name: str, model: str | None, as_json: bool) -> None:
    """Choose this connection's model as the first delegation route."""
    _emit(
        HandlerEndpointConnections().handle(
            _request(
                operation="select", connection_id=name, models=(model,) if model else ()
            )
        ),
        as_json,
    )


@click.command("remove-connection")
@click.argument("name")
@click.option("--yes", is_flag=True)
@click.option("--json", "as_json", is_flag=True)
def remove_connection_command(name: str, yes: bool, as_json: bool) -> None:
    """Remove only this connection's owned routes and credential."""
    if not yes:
        raise click.ClickException(
            "Review the connection to remove, then repeat with --yes."
        )
    _emit(
        HandlerEndpointConnections().handle(
            _request(operation="remove", connection_id=name)
        ),
        as_json,
    )


@click.command("test-connection")
@click.argument("name")
@click.option("--yes", is_flag=True)
@click.option("--json", "as_json", is_flag=True)
def test_connection_command(name: str, yes: bool, as_json: bool) -> None:
    """Prove every selected model through its pinned ONEX delegation route."""
    if not yes:
        raise click.ClickException(
            "Review the connection's inference cost, then repeat with --yes."
        )
    _emit(
        HandlerEndpointConnections().handle(
            _request(operation="test", connection_id=name)
        ),
        as_json,
    )
