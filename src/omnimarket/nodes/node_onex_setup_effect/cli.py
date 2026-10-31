# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""``onex setup``: set up this machine for ONEX, the first command a new developer runs.

    onex setup
    onex setup --workspace ~/omnibase --path docker

The command resolves the one value the walk cannot default, the workspace root,
from ``--workspace``, ``OMNIBASE_PATH``, ``OMNI_HOME`` or the root an earlier run
recorded, and prompts for it only when none names it and a terminal is attached.
It then hands the typed request to ``HandlerOnexSetup`` and prints the typed
result. A failed step exits 1 with its fix and the retry command.
"""

from __future__ import annotations

import sys
from pathlib import Path

import click
from omnibase_core.cli.cli_user_config import user_config_path

from omnimarket.nodes.node_onex_setup_effect.handlers.handler_onex_setup import (
    HandlerOnexSetup,
    recorded_state,
)
from omnimarket.nodes.node_onex_setup_effect.models import (
    ModelOnexSetupRequest,
    ModelOnexSetupResult,
)


def _report(result: ModelOnexSetupResult) -> None:
    for outcome in result.steps:
        status = "done" if outcome.changed else "unchanged"
        click.echo(f"[{status}] {outcome.step}: {outcome.detail}")


def _next_steps(workspace: Path, install_path: str) -> None:
    click.echo("")
    click.echo("Setup complete. Next:")
    click.echo(f'  export OMNIBASE_PATH="{workspace}"')
    if install_path == "docker":
        click.echo(
            "  start the self-hosted stack from an omnibase_infra checkout: "
            "make local-env && make up-local"
        )
    click.echo(
        '  onex delegate "<task>"   (the delegate command ships with omnibase_infra)'
    )
    click.echo("  onex doctor              (diagnose anything that looks wrong)")


@click.command("setup")
@click.option(
    "--workspace",
    type=click.Path(path_type=Path),
    envvar=["OMNIBASE_PATH", "OMNI_HOME"],
    default=None,
    help="Workspace root (default: $OMNIBASE_PATH, then $OMNI_HOME, then the root "
    "an earlier run recorded).",
)
@click.option(
    "--path",
    "install_path",
    type=click.Choice(["local", "docker", "cloud"]),
    default=None,
    help="Install path: local (default, no Docker) or docker (self-hosted stack). "
    "cloud is not available yet.",
)
@click.option(
    "--interactive/--no-interactive",
    default=None,
    help="Allow a prompt for a value that cannot be derived (default: only when "
    "run in a terminal).",
)
@click.pass_context
def setup_command(
    ctx: click.Context,
    workspace: Path | None,
    install_path: str | None,
    interactive: bool | None,
) -> None:
    """Set up this machine for ONEX: the documented first command for a new developer.

    Install the CLI, run onex setup, then start delegating. Setup resolves the
    workspace root, chooses the install path and checks what it needs, writes the
    user config (~/.onex/config.yaml) when there is none, and writes the workspace
    runtime configuration. It asks only for a value it cannot derive, and only in
    a terminal.

    Safe to re-run: a second run changes nothing. A failed step prints the one fix
    and the command to retry, and the retry resumes from that step.
    """
    if interactive is None:
        interactive = sys.stdin.isatty() and sys.stdout.isatty()
    onex_home = user_config_path().parent
    if workspace is None:
        state = recorded_state(onex_home)
        if state is not None:
            workspace = state.workspace
        elif interactive:
            workspace = Path(click.prompt("Workspace root (OMNIBASE_PATH)", type=str))
    request = ModelOnexSetupRequest(
        workspace=workspace, install_path=install_path, onex_home=onex_home
    )
    result = HandlerOnexSetup().handle(request)
    _report(result)
    if result.failure is not None:
        failure = result.failure
        click.echo(
            f"onex setup: step '{failure.step}' failed: {failure.problem}", err=True
        )
        click.echo(f"Fix: {failure.fix}", err=True)
        click.echo(f"Retry: {failure.retry}", err=True)
        click.echo(
            "Steps before this one are complete and are kept; the retry resumes here.",
            err=True,
        )
        ctx.exit(1)
    if result.workspace is None or result.install_path is None:
        raise click.ClickException("setup completed without a workspace or path")
    _next_steps(result.workspace, result.install_path)


__all__ = ["setup_command"]
