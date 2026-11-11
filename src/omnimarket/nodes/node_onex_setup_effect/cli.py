# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Select and review one typed setup plan, then run the shared setup engine.

Interactive and unattended commands use the same versioned state and adapters.
Verification observes without provisioning. Credentials enter only through hidden
input or stdin and are never included in a saved plan.
"""

from __future__ import annotations

import sys
from collections.abc import Callable
from functools import wraps
from getpass import getpass
from pathlib import Path
from shutil import get_terminal_size
from typing import Any

import click
from click.core import ParameterSource
from omnibase_core.cli.cli_user_config import user_config_path
from pydantic import SecretStr

from omnimarket.handlers.handler_endpoint_catalog import HandlerEndpointCatalog
from omnimarket.handlers.handler_endpoint_probe import HandlerModelProbe
from omnimarket.handlers.routing import byok_provider_backends
from omnimarket.nodes.node_onex_setup_effect.cli_choices import ask_choices
from omnimarket.nodes.node_onex_setup_effect.cli_workflow import run_workflow_cli
from omnimarket.nodes.node_onex_setup_effect.handlers.handler_onex_setup import (
    KEYLESS_PROVIDERS,
    recorded_state,
)
from omnimarket.nodes.node_onex_setup_effect.handlers.handler_setup_plugins import (
    HandlerSetupPlugins,
)
from omnimarket.nodes.node_onex_setup_effect.handlers.handler_setup_preflight import (
    HandlerSetupPreflight,
)
from omnimarket.nodes.node_onex_setup_effect.handlers.handler_setup_workflow import (
    HandlerSetupWorkflow,
)
from omnimarket.nodes.node_onex_setup_effect.models.model_setup_banner import (
    ModelSetupBanner,
)
from omnimarket.nodes.node_onex_setup_effect.models.model_setup_cli_failure import (
    ModelSetupCliFailure,
)
from omnimarket.nodes.node_onex_setup_effect.models.model_setup_plan import (
    ModelSetupPlan,
)

discover_models = HandlerEndpointCatalog().discover
probe_models = HandlerModelProbe().probe


def _json_failures[**P, R](function: Callable[P, R]) -> Callable[P, R]:
    @wraps(function)
    def invoke(*args: P.args, **kwargs: P.kwargs) -> R:
        try:
            return function(*args, **kwargs)
        except click.ClickException as error:
            if kwargs.get("json_output"):
                click.echo(
                    ModelSetupCliFailure(
                        message=error.format_message()
                    ).model_dump_json()
                )
                raise click.exceptions.Exit(error.exit_code) from None
            raise

    return invoke


def _workflow_cli(ctx: click.Context, **kwargs: Any) -> None:
    run_workflow_cli(
        ctx,
        workflow_factory=HandlerSetupWorkflow,
        onex_home=user_config_path().parent,
        read_key=_read_key,
        **kwargs,
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
        backend = byok_provider_backends.load_byok_provider_catalog().get(provider)
        if backend and backend.key_page:
            click.echo(f"Create or manage this provider's key: {backend.key_page}")
        value = getpass(f"Paste your {provider} API key (input hidden): ")
    else:
        value = sys.stdin.read()
    value = value.strip()
    hint = byok_provider_backends.key_provider_hint(value, provider)
    if hint is not None and hint != provider:
        raise click.ClickException(
            f"This key looks like {hint}, but {provider} was selected. Select the matching provider or enter its key. Nothing was stored."
        )
    return SecretStr(value) if value else None


def _ask_choices(
    provider: str | None,
    model: str | None,
    url: str | None,
    *,
    endpoint_auth: str | None = None,
    credentials: dict[str, SecretStr] | None = None,
    connection_id: str | None = None,
) -> tuple[str | None, str | None, str | None]:
    return ask_choices(
        provider,
        model,
        url,
        endpoint_auth=endpoint_auth,
        credentials=credentials,
        connection_id=connection_id,
        discover=discover_models,
        read_key=_read_key,
        probe=probe_models,
    )


class _SetupCommand(click.Command):
    """Keep parser failures typed before the setup callback can run."""

    def parse_args(self, ctx: click.Context, args: list[str]) -> list[str]:
        options = args[: args.index("--")] if "--" in args else args
        typed = "--json" in options and "--help" not in options
        try:
            return super().parse_args(ctx, args)
        except click.ClickException as error:
            if not typed:
                raise
            click.echo(
                ModelSetupCliFailure(
                    message="Invalid setup arguments. Review onex setup --help and provide the required option values."
                ).model_dump_json()
            )
            ctx.exit(error.exit_code)


@click.command("setup", cls=_SetupCommand)
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
    help="Choose a registered provider or an externally managed local/Ollama endpoint.",
)
@click.option("--model", default=None, help="The model the key, or the server, runs.")
@click.option(
    "--connection",
    "connection_id",
    default=None,
    help="Named server connection; keeps its endpoint, key and model order independent.",
)
@click.option(
    "--native-messages/--no-native-messages",
    default=None,
    help="Explicitly declare Anthropic Messages support for this named endpoint.",
)
@click.option(
    "--gateway-connection",
    default=None,
    help="Named connection with explicitly declared native Messages support.",
)
@click.option(
    "--gateway-model", default=None, help="Model for Claude gateway requests."
)
@click.option(
    "--gateway-port",
    type=click.IntRange(0, 65535),
    default=None,
    help="Loopback gateway port; 0 allocates an available port.",
)
@click.option(
    "--compose-file",
    type=click.Path(path_type=Path),
    help="Existing Compose file for optional local stack.",
)
@click.option(
    "--topology-file",
    type=click.Path(path_type=Path),
    help="Matching local stack topology file.",
)
@click.option(
    "--url",
    default=None,
    help="Base URL of an already running server; required for local, ollama and openai_compatible.",
)
@click.option(
    "--endpoint-auth",
    type=click.Choice(["none", "api-key"]),
    default=None,
    help="Ollama endpoint authentication (default: none); keys use hidden input or stdin.",
)
@click.option(
    "--preflight-only",
    is_flag=True,
    help="Read this Mac's requirements and exit without installing or changing files.",
)
@click.option(
    "--json", "json_output", is_flag=True, help="Emit only the typed result on stdout."
)
@click.option(
    "--action",
    type=click.Choice(["install", "verify", "repair", "configure", "upgrade"]),
)
@click.option(
    "--component",
    "components",
    multiple=True,
    type=str,
    help="Select an installed setup capability; repeat to select several.",
)
@click.option(
    "--plan",
    "plan_file",
    type=click.Path(path_type=Path),
    help="Secret-free typed JSON plan; cannot be combined with inline selections.",
)
@click.option(
    "--completion-name",
    default=None,
    help="Optional name for selected completion audio or notification.",
)
@click.option(
    "--developer",
    is_flag=True,
    help="Use the developer ONEX plugin profile from the selected workspace.",
)
@click.option(
    "--yes",
    is_flag=True,
    help="Accept the reviewed setup changes and selected model proof costs.",
)
@click.option(
    "--set-aside",
    is_flag=True,
    help="Back up and disable safe conflicting profile lines for this invocation only.",
)
@click.pass_context
@_json_failures
def setup_command(
    ctx: click.Context,
    workspace: Path | None,
    install_path: str | None,
    interactive: bool | None,
    provider: str | None,
    model: str | None,
    url: str | None,
    preflight_only: bool,
    json_output: bool,
    action: str | None,
    components: tuple[str, ...],
    plan_file: Path | None,
    yes: bool,
    developer: bool,
    endpoint_auth: str | None,
    connection_id: str | None,
    native_messages: bool | None,
    gateway_connection: str | None,
    gateway_model: str | None,
    gateway_port: int | None,
    compose_file: Path | None,
    topology_file: Path | None,
    completion_name: str | None,
    set_aside: bool,
) -> None:
    """Set up this machine for ONEX: the first command for a new developer.

    Choose components, provider models, and the workspace, then review the plan.
    Existing versioned choices are reused. --action verify observes the selected
    components without installing or changing their settings.

    Unattended: --no-interactive --yes --workspace DIR --provider P --model M
    [--url URL], with a hosted provider key on stdin. --plan FILE accepts a
    secret-free JSON plan. --json emits only the typed result on stdout.

    Setup rechecks each effect before skipping it. A failed component retains the
    completed steps and reports its fix; --action repair reconciles it on retry.
    """
    if endpoint_auth is not None and provider != "ollama":
        raise click.ClickException("--endpoint-auth is for --provider ollama.")
    if connection_id and provider and "," in provider:
        raise click.ClickException(
            "One named connection addresses one server. Use a plan for multiple connections."
        )
    if native_messages is not None and not connection_id:
        raise click.ClickException("--native-messages requires a named --connection.")
    if not json_output:
        click.echo(
            ModelSetupBanner.build(
                width=max(16, min(120, get_terminal_size().columns))
            ).text
        )
    if interactive is None:
        interactive = sys.stdin.isatty() and sys.stdout.isatty()
    interactive = interactive and not json_output
    if install_path == "cloud":
        raise click.ClickException(
            "Cloud setup is unavailable. Choose --path local or --path docker."
        )
    if preflight_only and (
        action is not None or components or plan_file is not None or developer
    ):
        raise click.ClickException(
            "Run --preflight-only separately from a setup action, component plan, or developer selection."
        )
    if preflight_only:
        observation = HandlerSetupPreflight().handle(
            ModelSetupPlan(
                workspace=workspace or Path.home() / "Code" / "omnibase",
                components=("delegation", "docker")
                if install_path == "docker"
                else ("delegation",),
                action="verify",
            )
        )
        if json_output:
            click.echo(observation.model_dump_json())
        else:
            click.echo(f"[{observation.status}] preflight")
            for name, value in observation.evidence.items():
                click.echo(f"  {name}: {value}")
            if observation.fix:
                click.echo(observation.fix, err=True)
        ctx.exit(0 if observation.status == "success" else 3)
    onex_home = user_config_path().parent
    try:
        saved = (
            HandlerSetupWorkflow.saved_plan(onex_home) if plan_file is None else None
        )
    except (OSError, ValueError):
        raise click.ClickException(
            "The saved setup state is invalid. Preserve it and review it before retrying."
        ) from None
    if plan_file is not None:
        if install_path is not None:
            raise click.ClickException("Inline --path cannot override --plan.")
        if ctx.get_parameter_source("workspace") == ParameterSource.ENVIRONMENT:
            workspace = None
    elif workspace is None:
        state = recorded_state(onex_home)
        if saved is not None:
            workspace = saved.workspace
        elif state is not None:
            workspace = state.workspace
        elif interactive:
            workspace = Path(click.prompt("Workspace root (OMNIBASE_PATH)", type=str))
        else:
            raise click.ClickException(
                "--workspace is missing. Pass --workspace DIR or export OMNIBASE_PATH=DIR."
            )
    prepared_credentials: dict[str, SecretStr] = {}
    if plan_file is None:
        if compose_file is not None or topology_file is not None:
            if compose_file is None or topology_file is None:
                raise click.ClickException(
                    "Supply both --compose-file and --topology-file."
                )
            components = tuple(
                dict.fromkeys((*(components or ("delegation",)), "local-stack"))
            )
        if gateway_connection or gateway_model or gateway_port is not None:
            if not gateway_connection or not gateway_model:
                raise click.ClickException(
                    "Supply both --gateway-connection and --gateway-model."
                )
            components = tuple(
                dict.fromkeys(
                    (*(components or ("delegation", "dashboard")), "claude-gateway")
                )
            )
        if install_path == "docker":
            components = tuple(
                dict.fromkeys((*(components or ("delegation",)), "docker"))
            )
        elif install_path == "local" and not components and saved is not None:
            components = tuple(item for item in saved.components if item != "docker")
        if interactive:
            if saved is not None and action is None:
                action = str(
                    click.prompt(
                        "Action",
                        type=click.Choice(
                            ["verify", "repair", "configure", "upgrade", "install"]
                        ),
                        default="verify",
                    )
                )
            if not components:
                chosen = str(
                    click.prompt(
                        "Components (comma-separated)",
                        default=",".join(
                            saved.components
                            if saved is not None
                            else HandlerSetupPlugins(
                                onex_home=onex_home
                            ).default_components()
                        ),
                    )
                )
                components = tuple(
                    item.strip() for item in chosen.split(",") if item.strip()
                )
            if (
                ("delegation" in (components or ("delegation",)))
                and action != "verify"
                and (provider is not None or saved is None)
            ):
                provider, model, url = _ask_choices(
                    provider,
                    model,
                    url,
                    endpoint_auth=endpoint_auth,
                    credentials=prepared_credentials,
                    connection_id=connection_id,
                )
    _workflow_cli(
        ctx,
        workspace=workspace,
        action=action,
        components=components,
        plan_file=plan_file,
        provider=provider,
        model=model,
        url=url,
        json_output=json_output,
        yes=yes,
        interactive=interactive,
        developer=developer,
        saved=saved,
        endpoint_auth=endpoint_auth,
        prepared_credentials=prepared_credentials,
        connection_id=connection_id,
        native_messages=native_messages,
        gateway_connection=gateway_connection,
        gateway_model=gateway_model,
        gateway_port=gateway_port,
        compose_file=compose_file,
        topology_file=topology_file,
        completion_name=completion_name,
        set_aside=set_aside,
    )


__all__ = ["setup_command"]
