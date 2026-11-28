# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""`onex stack`: read and stop the self-hosted stack that `/docker` starts (OMN-26689).

Both verbs act on the compose file shipped with this package, through the same
compose helpers the setup local-stack owner uses; nothing is typed by the user.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import click
from omnibase_infra.nodes.node_setup_local_provision_effect.handlers.handler_local_provision import (
    compose_base_command,
    run_compose,
)

COMPOSE_FILE = Path(__file__).parent / "stack" / "compose.yaml"

_STARTING = {"created", "restarting", "starting"}
_EXITED = {"exited", "dead", "removing"}


def _state(row: dict[str, object]) -> str:
    """One word per service: running, starting, exited, or compose's own word."""
    state = str(row.get("State", "")).lower()
    if state == "running" and str(row.get("Health", "")).lower() == "starting":
        return "starting"
    if state in _STARTING:
        return "starting"
    if state in _EXITED:
        return "exited"
    return state or "unknown"


def _rows(text: str) -> list[dict[str, object]]:
    """`docker compose ps --format json` prints one object per line, or one array."""
    try:
        document = json.loads(text)
        found = document if isinstance(document, list) else [document]
    except json.JSONDecodeError:
        found = []
        for line in text.splitlines():
            try:
                found.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return [row for row in found if isinstance(row, dict)]


@click.group("stack", help="The self-hosted stack that /docker starts.")
@click.pass_context
def stack_group(ctx: click.Context) -> None:
    """Read or stop the self-hosted stack."""
    ctx.ensure_object(dict)


@stack_group.command("status")
@click.option("--json", "json_output", is_flag=True, help="Print JSON.")
def status_command(json_output: bool) -> None:
    """Each service's state in the self-hosted stack."""
    code, _, out = asyncio.run(
        run_compose(
            [
                *compose_base_command(COMPOSE_FILE, None),
                "ps",
                "--all",
                "--format",
                "json",
            ]
        )
    )
    services = (
        [
            {"name": str(row.get("Service") or row.get("Name")), "state": _state(row)}
            for row in _rows(out)
        ]
        if code == 0
        else []
    )
    if json_output:
        click.echo(json.dumps({"stack": services}))
        return
    for service in services:
        click.echo(f"{service['name']}  {service['state']}")


@stack_group.command("stop")
def stop_command() -> None:
    """Stop the stack; its data volumes are kept."""
    code, err, _ = asyncio.run(
        run_compose([*compose_base_command(COMPOSE_FILE, None), "down"])
    )
    if code:
        raise click.ClickException(err or "The stack did not stop.")
    click.echo("The stack is stopped.")
