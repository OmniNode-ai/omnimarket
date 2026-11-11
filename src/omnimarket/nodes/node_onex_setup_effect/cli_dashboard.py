# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""CLI composition over the canonical setup dashboard lifecycle owner."""

from __future__ import annotations

import asyncio
import urllib.parse
from pathlib import Path

import click

from omnimarket.local_deployment.tenant_identity import read_local_tenant_identity
from omnimarket.nodes.node_local_dashboard_serve_effect.handlers.handler_local_dashboard_serve import (
    DASHBOARD_TOKEN_PARAM,
)


def _dashboard_lifecycle(action: str, json_output: bool) -> None:
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
        if action == "open" and observation.status == "success":
            from omnimarket.inference.local_byok_credential_adapter import (
                LocalByokCredentialStore,
            )

            token = asyncio.run(
                LocalByokCredentialStore().get_secret(
                    observation.evidence["credential_ref"]
                )
            )
            if not token:
                raise click.ClickException(
                    "Dashboard session is unavailable; run onex dashboard start."
                )
            url = (
                observation.evidence["url"]
                + "/?"
                + urllib.parse.urlencode({DASHBOARD_TOKEN_PARAM: token})
            )
            if click.launch(url) != 0:
                raise click.ClickException("The browser could not open the dashboard.")
    except (OSError, RuntimeError, ValueError) as error:
        raise click.ClickException(
            "Dashboard lifecycle failed; check its owned process, session and local identity."
        ) from error
    if json_output:
        click.echo(observation.model_dump_json())
    else:
        click.echo(f"Dashboard: {observation.status}")
        if observation.status == "success" and "url" in observation.evidence:
            click.echo(observation.evidence["url"])
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


@click.command("open")
@click.option("--json", "json_output", is_flag=True)
def dashboard_open(json_output: bool) -> None:
    """Open the running dashboard's authenticated session in the browser."""
    _dashboard_lifecycle("open", json_output)


def attach_dashboard_lifecycle(group: click.Group) -> None:
    for command in (dashboard_start, dashboard_status, dashboard_stop, dashboard_open):
        group.add_command(command)
