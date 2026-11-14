# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""``onex setup``: set up this machine for ONEX, the first command a new developer runs.

    onex setup
    onex setup --workspace ~/omnibase --path docker
    printf %s "$KEY" | onex setup --no-interactive --workspace DIR --provider openrouter --model M
    onex setup --no-interactive --workspace DIR --provider local --url http://host:8080 --model M

The last step stores the key you give (hidden at a prompt, or on stdin; never an
argument), runs one delegation pinned to it and prints its receipt. The unattended
form names every choice and refuses, before any step runs, the one that is missing.

The command resolves the one value the walk cannot default, the workspace root,
from ``--workspace``, ``OMNIBASE_PATH``, ``OMNI_HOME`` or the root an earlier run
recorded, and prompts for it only when none names it and a terminal is attached.
It then hands the typed request to ``HandlerOnexSetup`` and prints the typed
result. A failed step exits 1 with its fix and the retry command.
"""

from __future__ import annotations

import sys
from getpass import getpass
from pathlib import Path

import click
from omnibase_core.cli.cli_user_config import user_config_path
from pydantic import SecretStr

from omnimarket.nodes.node_onex_setup_effect.handlers.handler_onex_setup import (
    KEYLESS_PROVIDERS,
    REFUSED_PROVIDERS,
    SETUP_PROVIDERS,
    URL_PROVIDERS,
    HandlerOnexSetup,
    recorded_state,
)
from omnimarket.nodes.node_onex_setup_effect.models import (
    ModelOnexSetupRequest,
    ModelOnexSetupResult,
)

# Printed before the first model question (and before any step when unattended):
# an exhausted model ladder ends returned_to_caller, so Claude Code does the task.
LADDER_NOTICE = (
    "If none of the models you choose can finish a task, free or paid, the task "
    "goes back to Claude Code and Claude does it as usual. That task shows no "
    "savings, and any attempts your models made are still billed by their provider."
)


def _report(result: ModelOnexSetupResult) -> None:
    for outcome in result.steps:
        status = "done" if outcome.changed else "unchanged"
        click.echo(f"[{status}] {outcome.step}: {outcome.detail}")
    if result.receipt is not None:
        receipt = result.receipt
        click.echo(
            f"Receipt: provider={receipt.provider} model={receipt.model} "
            f"backend={receipt.backend_id or '-'} tested_at={receipt.tested_at}"
        )


def _read_key(provider: str | None, interactive: bool) -> SecretStr | None:
    """The key: hidden at a terminal, else the first line piped on stdin. Never echoed.

    An unattended run with a terminal on stdin has no piped key, and waiting for one
    would hang a script, so it reads none and the handler refuses it by name.
    """
    if provider is None or provider.lower() in KEYLESS_PROVIDERS:
        return None
    if sys.stdin.isatty():
        if not interactive:
            return None
        value = getpass(f"Paste your {provider} API key (input hidden): ")
    else:
        value = sys.stdin.read()
    value = value.strip()
    return SecretStr(value) if value else None


def _ask_choices(
    provider: str | None, model: str | None, url: str | None
) -> tuple[str | None, str | None, str | None]:
    """Prompt for the provider, model and URL a terminal run was not given."""
    if provider is None:
        provider = str(
            click.prompt(
                "Provider",
                type=click.Choice(SETUP_PROVIDERS),
                default="openrouter",
            )
        )
    if provider in URL_PROVIDERS and url is None:
        url = str(click.prompt("Server base URL (http://<host>:<port>)", type=str))
    if model is None:
        model = str(click.prompt("Model", type=str)).strip()
    return provider, model, url


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
@click.option(
    "--provider",
    default=None,
    help="Model provider: gemini, openrouter, openai, openai_compatible or local. "
    "Ollama is reached as --provider local --url.",
)
@click.option("--model", default=None, help="The model the key, or the server, runs.")
@click.option(
    "--url",
    default=None,
    help="Base URL of the server; required for local and openai_compatible.",
)
@click.pass_context
def setup_command(
    ctx: click.Context,
    workspace: Path | None,
    install_path: str | None,
    interactive: bool | None,
    provider: str | None,
    model: str | None,
    url: str | None,
) -> None:
    """Set up this machine for ONEX: the documented first command for a new developer.

    Install the CLI, run onex setup, then start delegating. Setup resolves the
    workspace root, chooses the install path and checks what it needs, writes the
    user config (~/.onex/config.yaml) when there is none, and writes the workspace
    runtime configuration. It then asks for a provider, a model and your key
    (hidden), stores the key the way onex models add does, runs one delegation and
    prints its receipt. It asks only in a terminal.

    Unattended: onex setup --no-interactive --workspace DIR --provider P --model M
    [--url URL], with the key on stdin; --url is required for local and
    openai_compatible. Any choice left out is refused before a step runs.

    Safe to re-run: the setup steps change nothing a second time; the model step
    stores the key again and proves a delegation again. A failed step prints the one
    fix and the command to retry, and the retry resumes from that step.
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
    if provider is not None and provider.lower() in REFUSED_PROVIDERS:
        interactive = False  # refused by name below; never prompt for it
    click.echo(LADDER_NOTICE)
    if interactive:
        provider, model, url = _ask_choices(provider, model, url)
    request = ModelOnexSetupRequest(
        workspace=workspace,
        install_path=install_path,
        onex_home=onex_home,
        provider=provider.lower() if provider else None,
        model=model,
        url=url,
        key=_read_key(provider, interactive),
        unattended=not interactive,
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
        if request.key is not None:
            click.echo(
                "The key was not kept in the command: give it again on stdin.", err=True
            )
        if result.steps:
            click.echo(
                "Steps before this one are complete and are kept; the retry resumes here.",
                err=True,
            )
        else:
            click.echo("No step ran and nothing was written.", err=True)
        ctx.exit(1)
    if result.workspace is None or result.install_path is None:
        raise click.ClickException("setup completed without a workspace or path")
    _next_steps(result.workspace, result.install_path)


__all__ = ["setup_command"]
