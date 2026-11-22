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
from importlib import resources
from pathlib import Path
from shutil import get_terminal_size
from typing import Any

import click
import yaml
from click.core import ParameterSource
from omnibase_core.cli.cli_user_config import user_config_path
from omnibase_core.errors.model_onex_error import ModelOnexError
from omnibase_core.models.event_bus.model_run_node_hosted import ModelRunNodeHosted
from pydantic import SecretStr

from omnimarket.handlers.handler_endpoint_catalog import HandlerEndpointCatalog
from omnimarket.handlers.handler_endpoint_probe import HandlerModelProbe
from omnimarket.handlers.routing import byok_provider_backends
from omnimarket.nodes.node_onex_setup_effect.cli_choices import (
    ask_choices,
    stored_key_for_unchanged_provider,
)
from omnimarket.nodes.node_onex_setup_effect.cli_workflow import run_workflow_cli
from omnimarket.nodes.node_onex_setup_effect.handlers.handler_onex_setup import (
    KEYLESS_PROVIDERS,
    discard_key,
    park_key,
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
from omnimarket.nodes.node_onex_setup_effect.models import (
    ModelOnexSetupRequest,
    ModelOnexSetupResult,
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
from omnimarket.nodes.node_onex_setup_effect.models.model_setup_provider_traits import (
    setup_provider_traits,
)

discover_models = HandlerEndpointCatalog().discover
probe_models = HandlerModelProbe().probe

# Printed before the first model question (and before any step when unattended):
# an exhausted model ladder ends returned_to_caller, so Claude Code does the task.
LADDER_NOTICE = (
    "If none of the models you choose can finish a task, free or paid, the task "
    "goes back to Claude Code and Claude does it as usual. That task shows no "
    "savings, and any attempts your models made are still billed by their provider."
)


def _json_failures(function: Callable[..., None]) -> Callable[..., None]:
    """A click command whose refusal is one JSON line when --json is given."""

    @wraps(function)
    def invoke(*args: object, **kwargs: object) -> None:
        try:
            function(*args, **kwargs)
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


NODE_PACKAGE = "omnimarket.nodes.node_onex_setup_effect"
NODE_NAME = "node_onex_setup_effect"


def _run_through_runtime(request: ModelOnexSetupRequest) -> ModelOnexSetupResult:
    """Publish the walk's command on the local runtime and read its terminal result.

    The contract names the topic and the time the walk may take (``descriptor.timeout_ms``).
    """
    contract_path = Path(str(resources.files(NODE_PACKAGE) / "contract.yaml"))
    contract = yaml.safe_load(contract_path.read_text(encoding="utf-8"))
    timeout_s = int(contract["descriptor"]["timeout_ms"]) // 1000
    token = park_key(request.key) if request.key is not None else None
    payload = request.model_dump(mode="json", exclude={"key"})
    payload["key_handoff"] = token
    try:
        produced = ModelRunNodeHosted.run(NODE_NAME, contract_path, payload, timeout_s)
    except ModelOnexError as error:
        raise click.ClickException(error.message) from error
    finally:
        discard_key(token)
    return ModelOnexSetupResult.model_validate(produced)


def _setup_through_runtime(ctx: click.Context, request: ModelOnexSetupRequest) -> None:
    """Run the setup command on the local runtime and report it; a failed step exits 1
    with its fix and retry (OMN-24455)."""
    result = _run_through_runtime(request)
    _report(result)
    if result.failure is None:
        return
    failure = result.failure
    click.echo(f"onex setup: step '{failure.step}' failed: {failure.problem}", err=True)
    click.echo(f"Fix: {failure.fix}", err=True)
    click.echo(f"Retry: {failure.retry}", err=True)
    if request.key is not None:
        click.echo(
            "The key was not kept in the command: give it again on stdin.", err=True
        )
    click.echo(
        "Steps before this one are complete and are kept; the retry resumes here."
        if result.steps
        else "No step ran and nothing was written.",
        err=True,
    )
    ctx.exit(1)


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
    saved: ModelSetupPlan | None = None,
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
        default_provider=saved.providers[0].provider
        if saved is not None and saved.providers
        else None,
        stored_key=lambda name, address: stored_key_for_unchanged_provider(
            saved, name, address
        ),
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
            raise click.exceptions.Exit(error.exit_code) from error


def current_folder_workspace(cwd: Path, home: Path) -> Path | None:
    """OMN-23413 AC19: the folder setup runs from is the workspace, as in
    coding-agent CLIs, except the home folder or the filesystem root, which
    fall back to $OMNIBASE_PATH, the recorded root or a prompt."""
    folder = cwd.resolve()
    if folder == home.resolve() or folder == Path(folder.anchor):
        return None
    return folder


def linked_workspace_notice(workspace: Path) -> str | None:
    """The line naming the real folder a workspace reached through a link writes to.

    OMN-23413 (operator decision 2026-11-22): a workspace given as a link is
    followed as the user's choice, and setup says which real folder it writes
    to. ``..`` is never collapsed as text: setup resolves links first and then
    ``..``, so the real folder is ``resolve()``'s. The path itself or any folder
    above it may be the link, a dangling one included. None when no link is on
    the path.
    """
    given = workspace.expanduser()
    if not given.is_absolute():
        given = Path.cwd() / given
    prefixes = (Path(*given.parts[:end]) for end in range(2, len(given.parts) + 1))
    if not any(prefix.is_symlink() for prefix in prefixes):
        return None
    return (
        f"Workspace: {given} reaches its real folder {given.resolve()} through a link."
    )


@click.command("setup", cls=_SetupCommand)
@click.option(
    "--workspace",
    type=click.Path(path_type=Path),
    envvar="OMNIBASE_PATH",
    default=None,
    help="Workspace root (default: the current folder; from the home folder or / "
    "it is $OMNIBASE_PATH, then the root an earlier run recorded).",
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
    help="Choose a registered provider, or local: a Local Model, any model server "
    "you run, by its endpoint --url.",
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
    help="Base URL of an already running server; required for local and openai_compatible.",
)
@click.option(
    "--endpoint-auth",
    type=click.Choice(["none", "api-key"]),
    default=None,
    help="A Local Model server's authentication (default: none); keys use hidden input or stdin.",
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
    if (
        endpoint_auth is not None
        and provider not in setup_provider_traits().endpoint_auth
    ):
        raise click.ClickException("--endpoint-auth is for --provider local.")
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
    if (
        plan_file is None
        and ctx.get_parameter_source("workspace") is not ParameterSource.COMMANDLINE
    ):
        workspace = current_folder_workspace(Path.cwd(), Path.home()) or workspace
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
    # OMN-21262 (merged from dev): before the first model question, and before
    # any step in an unattended run. Under --json it goes to stderr, so stdout
    # stays the one typed result.
    if workspace is not None:
        notice = linked_workspace_notice(workspace)
        if notice is not None:
            click.echo(notice, err=json_output)
    click.echo(LADDER_NOTICE, err=json_output)
    prepared_credentials: dict[str, SecretStr] = {}
    # A configure re-run with no --provider adds to the saved providers (OMN-23825).
    added_to: ModelSetupPlan | None = None
    if plan_file is None:
        if compose_file is not None or topology_file is not None:
            if compose_file is None or topology_file is None:
                raise click.ClickException(
                    "Supply both --compose-file and --topology-file."
                )
            components = tuple(
                dict.fromkeys((*(components or ("delegation",)), "local-stack"))
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
                and (provider is not None or saved is None or action == "configure")
            ):
                if provider is None and action == "configure":
                    # Only the added provider changes: every saved choice stays,
                    # the workspace too unless --workspace names another.
                    added_to = saved
                    if (
                        saved is not None
                        and ctx.get_parameter_source("workspace")
                        is not ParameterSource.COMMANDLINE
                    ):
                        workspace = saved.workspace
                provider, model, url = _ask_choices(
                    provider,
                    model,
                    url,
                    endpoint_auth=endpoint_auth,
                    credentials=prepared_credentials,
                    connection_id=connection_id,
                    # Only a configure re-run reuses a stored key: a named
                    # --provider can always take a new (rotated) key.
                    saved=added_to,
                )
    if (
        not interactive
        and components == ("delegation",)
        and plan_file is None
        and action is None
        and not json_output
        and not developer
        and install_path in (None, "local")
        and endpoint_auth is None
        and connection_id is None
        and native_messages is None
        and completion_name is None
        and not set_aside
    ):
        # OMN-24455: the unattended delegation setup is the node's command on the
        # local runtime, which records it; HandlerOnexSetup runs this same workflow.
        _setup_through_runtime(
            ctx,
            ModelOnexSetupRequest(
                workspace=workspace,
                install_path=install_path,
                onex_home=onex_home,
                provider=provider.lower() if provider else None,
                model=model,
                url=url,
                key=_read_key(provider, interactive),
                unattended=True,
            ),
        )
        return
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
        added_to=added_to,
        connection_id=connection_id,
        native_messages=native_messages,
        compose_file=compose_file,
        topology_file=topology_file,
        completion_name=completion_name,
        set_aside=set_aside,
    )


__all__ = ["setup_command"]
