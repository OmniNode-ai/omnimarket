# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Thin contributions to the core-owned, extensible ``onex config`` group."""

from __future__ import annotations

from pathlib import Path

import click

from omnimarket.nodes.node_onex_setup_effect.handlers.handler_setup_configuration import (
    HandlerSetupConfiguration,
)
from omnimarket.nodes.node_onex_setup_effect.models.model_setup_configuration import (
    ModelSetupConfigurationRequest,
    ModelSetupConfigurationResult,
)


def _run(**values: object) -> ModelSetupConfigurationResult:
    try:
        request = ModelSetupConfigurationRequest.model_validate(values)
        return HandlerSetupConfiguration().handle(request)
    except (OSError, ValueError):
        return ModelSetupConfigurationResult(
            status="refused",
            message="Invalid configuration request. Use a valid profile name and readable configuration file; secrets belong in the credential store.",
        )


def _report(result: ModelSetupConfigurationResult, *, json_output: bool) -> None:
    if json_output:
        click.echo(result.model_dump_json())
    elif result.status != "ready":
        click.echo(result.message or "Configuration is unavailable.", err=True)
    else:
        if result.profile is not None:
            click.echo(result.profile.model_dump_json(indent=2))
        elif result.profiles:
            click.echo("\n".join(result.profiles))
        if result.source is not None:
            click.echo(f"Source: {result.source}")
        if result.message:
            click.echo(result.message)
    if result.status != "ready":
        raise click.exceptions.Exit(1)


@click.command("validate")
@click.argument("source", type=click.Path(path_type=Path), required=False)
@click.option("--json", "json_output", is_flag=True)
def validate_command(source: Path | None, json_output: bool) -> None:
    """Validate a file or the saved setup choices without applying effects."""
    _report(_run(operation="validate", source=source), json_output=json_output)


@click.command("export")
@click.option("--profile", "name")
def export_command(name: str | None) -> None:
    """Write a portable profile as JSON; defaults to the saved setup plan."""
    result = _run(operation="export", name=name)
    if result.status == "ready" and result.profile is not None:
        click.echo(result.profile.model_dump_json(indent=2))
    else:
        _report(result, json_output=True)


@click.command("import")
@click.argument("source", type=click.Path(path_type=Path))
@click.option("--name", required=True)
@click.option("--replace", is_flag=True)
@click.option("--json", "json_output", is_flag=True)
def import_command(source: Path, name: str, replace: bool, json_output: bool) -> None:
    """Save desired choices as a named profile; this does not run setup."""
    _report(
        _run(operation="import", source=source, name=name, replace=replace),
        json_output=json_output,
    )


profiles_group = click.Group(
    "profiles", help="Inspect saved desired configuration profiles."
)


@profiles_group.command("list")
@click.option("--json", "json_output", is_flag=True)
def list_profiles_command(json_output: bool) -> None:
    """List valid profiles without applying or changing them."""
    _report(_run(operation="list"), json_output=json_output)


@click.command("effective")
@click.option("--json", "json_output", is_flag=True)
def effective_command(json_output: bool) -> None:
    """Show the canonical saved setup plan and its source path."""
    _report(_run(operation="export"), json_output=json_output)
