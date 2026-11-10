# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Select and review one typed setup plan, then run the shared setup engine.

Interactive and unattended commands use the same versioned state and adapters.
Verification observes without provisioning. Credentials enter only through hidden
input or stdin and are never included in a saved plan.
"""

from __future__ import annotations

import os
import stat
import sys
from getpass import getpass
from pathlib import Path
from shutil import get_terminal_size

import click
from click.core import ParameterSource
from omnibase_core.cli.cli_user_config import user_config_path
from pydantic import SecretStr, ValidationError

from omnimarket.nodes.node_onex_setup_effect.handlers.handler_onex_setup import (
    KEYLESS_PROVIDERS,
    SETUP_PROVIDERS,
    URL_PROVIDERS,
    recorded_state,
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
from omnimarket.nodes.node_onex_setup_effect.models.model_setup_plan import (
    ModelSetupPlan,
)


def _workflow_cli(
    ctx: click.Context,
    *,
    workspace: Path | None,
    action: str | None,
    components: tuple[str, ...],
    plan_file: Path | None,
    provider: str | None,
    model: str | None,
    url: str | None,
    json_output: bool,
    yes: bool,
    interactive: bool,
    developer: bool,
    saved: ModelSetupPlan | None,
) -> None:
    if plan_file is None:
        if provider and not model:
            raise click.ClickException(
                "--model is missing. Name the selected provider's ordered models."
            )
        if not provider and (model or url):
            raise click.ClickException(
                "--provider is missing. Name the provider for these model selections."
            )
        if provider in URL_PROVIDERS and not url:
            raise click.ClickException(f"--url is missing for --provider {provider}.")
    try:
        if plan_file is not None:
            if (
                workspace is not None
                or action is not None
                or components
                or provider
                or model
                or url
                or developer
            ):
                raise ValueError("Plan and inline selections conflict")
            fd = os.open(plan_file, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(fd, "rb") as source:
                info = os.fstat(source.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_size > 1024 * 1024:
                    raise ValueError("Invalid plan file")
                raw = source.read(1024 * 1024 + 1)
            plan = ModelSetupPlan.model_validate_json(raw)
        else:
            providers = [
                {
                    "provider": name.strip(),
                    "models": tuple(
                        item.strip()
                        for item in (model or "").split(",")
                        if item.strip()
                    ),
                    "url": url,
                }
                for name in (provider or "").split(",")
                if name.strip()
            ]
            if not provider and (model or url):
                raise ValueError("Name the provider for inline model selections")
            reuse = saved is not None and workspace == saved.workspace
            plan = ModelSetupPlan.model_validate(
                {
                    "workspace": workspace or Path.home() / "Code" / "omnibase",
                    "action": action or "install",
                    "components": components
                    or (
                        saved.components
                        if saved is not None and reuse
                        else ("delegation",)
                    ),
                    "providers": providers
                    or (
                        saved.providers
                        if saved is not None and reuse and provider is None
                        else ()
                    ),
                    "developer": developer
                    or (saved.developer if saved is not None and reuse else False),
                }
            )
        unsupported = set(plan.components) - HandlerSetupWorkflow.supported_components()
        if unsupported:
            raise click.ClickException(
                "Setup adapters are not integrated yet for: "
                + ", ".join(sorted(unsupported))
                + ". Nothing was installed."
            )
    except (OSError, ValueError, ValidationError):
        raise click.ClickException(
            "Invalid or conflicting setup selections. Use a bounded secret-free JSON plan with supported components; inline flags cannot override --plan."
        ) from None
    if any(item.provider == "ollama" for item in plan.providers):
        from omnimarket.nodes.node_onex_setup_effect.handlers.handler_setup_local_runtime import (
            HandlerSetupLocalRuntime,
        )

        try:
            HandlerSetupLocalRuntime().validate_selection(plan)
        except ValueError as exc:
            raise click.ClickException(str(exc)) from None
    credentials: dict[str, SecretStr] = {}
    if plan.action != "verify":
        if "delegation" in plan.components and not plan.providers:
            raise click.ClickException(
                "Choose provider models before installation using --provider/--model or --plan."
            )
        if not yes and not interactive:
            raise click.ClickException(
                "Review the setup destination, selected models and unknown/API billing costs, then repeat with --yes."
            )
        keyed = [
            selection.provider
            for selection in plan.providers
            if selection.provider not in ("local", "ollama")
        ]
        if not sys.stdin.isatty() and len(keyed) > 1:
            raise click.ClickException(
                "Multiple provider keys require hidden terminal prompts or preconfigured canonical store references."
            )
        for name in keyed:
            key = _read_key(name, interactive)
            if key is None:
                raise click.ClickException(
                    "A selected provider needs a credential on hidden input or stdin. Nothing was installed."
                )
            credentials[name] = key
        if not yes:
            click.echo(
                f"Workspace: {plan.workspace}\nAction: {plan.action}\nComponents: {', '.join(plan.components)}"
            )
            for selection in plan.providers:
                click.echo(
                    f"Models: {selection.provider}: {', '.join(selection.models)}; price: unknown"
                )
            if not click.confirm(
                "Apply this setup and run its selected model proofs, accepting any provider API charges?",
                default=False,
            ):
                raise click.Abort()
    journal = HandlerSetupWorkflow(
        onex_home=user_config_path().parent, credentials=credentials
    ).handle(plan)
    if json_output:
        click.echo(journal.model_dump_json())
    else:
        for item in journal.observations:
            click.echo(
                f"[{item.status}] {item.step} ({'changed' if item.changed else 'unchanged'})"
            )
            for label, value in item.evidence.items():
                click.echo(f"  {label}: {value}")
            for receipt in item.receipts:
                click.echo(
                    f"Receipt: provider={receipt.provider} model={receipt.model} backend={receipt.backend_id or '-'} tested_at={receipt.tested_at}"
                )
            if item.fix:
                click.echo(f"Fix: {item.fix}", err=True)
            if item.retry:
                click.echo(f"Retry: {item.retry}", err=True)
                if credentials:
                    click.echo(
                        "Provide the required key again via hidden input or stdin; it is not saved in the retry command.",
                        err=True,
                    )
        if all(item.status in ("success", "skipped") for item in journal.observations):
            click.echo("Setup complete.")
    ctx.exit(
        0
        if all(item.status in ("success", "skipped") for item in journal.observations)
        else 1
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
    type=click.Choice(
        ["delegation", "dashboard", "claude-plugin", "claude-hooks", "docker"]
    ),
)
@click.option(
    "--plan",
    "plan_file",
    type=click.Path(path_type=Path),
    help="Secret-free typed JSON plan; cannot be combined with inline selections.",
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
@click.pass_context
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
    if plan_file is None:
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
                            else (
                                "delegation",
                                "dashboard",
                                "claude-plugin",
                                "claude-hooks",
                            )
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
                provider, model, url = _ask_choices(provider, model, url)
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
    )


__all__ = ["setup_command"]
