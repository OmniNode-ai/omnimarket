# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""CLI composition over the canonical setup dashboard lifecycle owner."""

from __future__ import annotations

import json
import urllib.parse
from pathlib import Path

import click
import yaml
from omnibase_core.errors.model_onex_error import ModelOnexError
from omnibase_infra.gateway.client.store_gateway_credential import (
    StoreGatewayCredential,
)

from omnimarket.local_deployment.tenant_identity import read_local_tenant_identity


def _dashboard_lifecycle(
    action: str, json_output: bool, correlation_id: str | None = None
) -> None:
    from omnimarket.nodes.node_onex_setup_effect.handlers.handler_setup_dashboard import (
        HandlerSetupDashboard,
    )
    from omnimarket.nodes.node_onex_setup_effect.models.model_setup_observation import (
        ModelSetupObservation,
    )
    from omnimarket.nodes.node_onex_setup_effect.models.model_setup_plan import (
        ModelSetupPlan,
    )

    owner = HandlerSetupDashboard()
    plan = ModelSetupPlan(
        workspace=Path.home(), components=("dashboard",), action="verify"
    )
    try:
        if action == "start":
            if read_local_tenant_identity() is None:
                raise click.ClickException(
                    "Run onex local init or onex setup before starting the dashboard."
                )
            owner.apply(plan)
        elif action == "stop":
            owner.stop()
            observation = ModelSetupObservation(
                step="dashboard", status="success", evidence={"running": "false"}
            )
        if action != "stop":
            observation = owner.observe(plan)
    except (OSError, RuntimeError, ValueError) as error:
        raise click.ClickException(
            "Dashboard lifecycle failed; check its owned process, session and local identity."
        ) from error
    if json_output:
        click.echo(observation.model_dump_json())
    else:
        click.echo(f"Dashboard: {observation.status}")
        if observation.status == "success":
            click.echo("Run `onex dashboard open` to open the hosted dashboard.")
        elif observation.fix:
            click.echo(observation.fix, err=True)
    if observation.status != "success":
        raise click.exceptions.Exit(1)


@click.command("start")
@click.option("--json", "json_output", is_flag=True)
def dashboard_start(json_output: bool) -> None:
    """Start the authenticated local dashboard, or reuse its healthy process."""
    _dashboard_lifecycle("start", json_output)


@click.command("status")
@click.option("--json", "json_output", is_flag=True)
def dashboard_status(json_output: bool) -> None:
    """Read the dashboard's current process, session and tenant readiness."""
    _dashboard_lifecycle("status", json_output)


@click.command("stop")
@click.option("--json", "json_output", is_flag=True)
def dashboard_stop(json_output: bool) -> None:
    """Stop only the recorded ONEX dashboard process."""
    _dashboard_lifecycle("stop", json_output)


_NOT_SIGNED_IN = "You are not signed in. Run `onex login` first."
_NO_ADDRESS = (
    "No hosted dashboard address is configured. Set dashboard.url in "
    "~/.onex/config.yaml."
)


def _onex_root() -> Path:
    return Path.home() / ".onex"


def hosted_dashboard_url(onex_root: Path) -> str | None:
    """The hosted dashboard address for the selected environment, from configuration."""
    configured = ""
    config = onex_root / "config.yaml"
    if config.is_file():
        document = yaml.safe_load(config.read_text(encoding="utf-8"))
        block = document.get("dashboard") if isinstance(document, dict) else None
        value = block.get("url") if isinstance(block, dict) else None
        configured = value.strip() if isinstance(value, str) else ""
    return configured.rstrip("/") or None


@click.command("open")
@click.option("--json", "json_output", is_flag=True)
@click.option(
    "--correlation-id",
    "correlation_id",
    default=None,
    help="Open this run's Workflow page instead of the front page.",
)
def dashboard_open(json_output: bool, correlation_id: str | None) -> None:
    """Open the hosted dashboard for the selected environment in the browser."""
    if correlation_id is not None:
        correlation_id = correlation_id.strip()
        if not correlation_id:
            raise click.UsageError("--correlation-id must not be blank")
    root = _onex_root()
    try:
        StoreGatewayCredential(onex_home=root).load()
    except ModelOnexError as error:
        raise click.ClickException(_NOT_SIGNED_IN) from error
    base = hosted_dashboard_url(root)
    if base is None:
        raise click.ClickException(_NO_ADDRESS)
    url = base + "/"
    if correlation_id is not None:
        url = (
            base
            + "/workflow?"
            + urllib.parse.urlencode({"correlation_id": correlation_id})
        )
    if click.launch(url) != 0:
        raise click.ClickException("The browser could not open the dashboard.")
    if json_output:
        click.echo(json.dumps({"opened": url}))
    else:
        click.echo(f"Dashboard: opened {url}")


def attach_dashboard_lifecycle(group: click.Group) -> None:
    for command in (dashboard_start, dashboard_status, dashboard_stop, dashboard_open):
        group.add_command(command)
