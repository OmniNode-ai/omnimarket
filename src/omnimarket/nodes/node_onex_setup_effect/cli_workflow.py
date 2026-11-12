# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed setup plan intake and workflow invocation shared by CLI choices."""

from __future__ import annotations

import sys
from collections.abc import Callable
from pathlib import Path

import click
from pydantic import SecretStr, ValidationError

from omnimarket.handlers.handler_endpoint_connections import (
    HandlerModelConnections,
    ModelEndpointConnectionRequest,
)
from omnimarket.nodes.node_onex_setup_effect.handlers.handler_onex_setup import (
    URL_PROVIDERS,
)
from omnimarket.nodes.node_onex_setup_effect.handlers.handler_setup_configuration import (
    HandlerSetupConfiguration,
)
from omnimarket.nodes.node_onex_setup_effect.handlers.handler_setup_plugins import (
    HandlerSetupPlugins,
)
from omnimarket.nodes.node_onex_setup_effect.handlers.handler_setup_workflow import (
    HandlerSetupWorkflow,
)
from omnimarket.nodes.node_onex_setup_effect.models.model_setup_plan import (
    ModelSetupPlan,
)


def run_workflow_cli(
    ctx: click.Context,
    *,
    workflow_factory: type[HandlerSetupWorkflow],
    onex_home: Path,
    read_key: Callable[[str | None, bool], SecretStr | None],
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
    endpoint_auth: str | None,
    prepared_credentials: dict[str, SecretStr] | None = None,
    connection_id: str | None = None,
    native_messages: bool | None = None,
    gateway_connection: str | None = None,
    gateway_model: str | None = None,
    gateway_port: int | None = None,
    compose_file: Path | None = None,
    topology_file: Path | None = None,
    completion_name: str | None = None,
    set_aside: bool = False,
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
                or endpoint_auth
                or connection_id
                or native_messages is not None
                or gateway_connection
                or gateway_model
                or gateway_port is not None
                or compose_file is not None
                or topology_file is not None
                or completion_name is not None
            ):
                raise ValueError("Plan and inline selections conflict")
            plan = HandlerSetupConfiguration.load_plan(plan_file)
            if set_aside:
                plan = plan.model_copy(update={"set_aside": True})
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
                    "api_key_required": endpoint_auth == "api-key",
                    "connection_id": connection_id,
                    "native_messages": native_messages,
                    "make_default": connection_id is not None,
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
                    "set_aside": set_aside,
                    "completion_name": completion_name
                    if completion_name is not None
                    else (
                        saved.completion_name if saved is not None and reuse else None
                    ),
                    "local_stack": (
                        {"compose_file": compose_file, "topology_file": topology_file}
                        if compose_file is not None or topology_file is not None
                        else (
                            saved.local_stack if saved is not None and reuse else None
                        )
                    ),
                    "gateway": (
                        {
                            "connection_id": gateway_connection,
                            "model": gateway_model,
                            "port": gateway_port or 0,
                        }
                        if gateway_connection
                        or gateway_model
                        or gateway_port is not None
                        else (saved.gateway if saved is not None and reuse else None)
                    ),
                }
            )
        unsupported = set(plan.components) - workflow_factory.supported_components()
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
    credentials: dict[str, SecretStr] = dict(prepared_credentials or {})
    if plan.action != "verify":
        if "delegation" in plan.components and not plan.providers:
            raise click.ClickException(
                "Choose provider models before installation using --provider/--model or --plan."
            )
        if not yes and not interactive:
            raise click.ClickException(
                "Review the setup destination, selected models and unknown/API billing costs, then repeat with --yes."
            )
        keyed = [selection for selection in plan.providers if selection.needs_key]
        pending_keys = []
        for selection in keyed:
            identity = selection.credential_identity
            if identity in credentials:
                continue
            if selection.connection_id:
                configured = (
                    HandlerModelConnections()
                    .handle(
                        ModelEndpointConnectionRequest(
                            operation="list", connection_id=selection.connection_id
                        )
                    )
                    .connections
                )
                if (
                    configured
                    and configured[0].secret_ref
                    and configured[0].url
                    == (selection.url or "").rstrip("/").removesuffix("/v1")
                    and not interactive
                ):
                    continue
            pending_keys.append(selection)
        if not sys.stdin.isatty() and len(pending_keys) > 1:
            raise click.ClickException(
                "Multiple provider keys require hidden terminal prompts or preconfigured canonical store references."
            )
        for selection in pending_keys:
            name = selection.provider
            identity = selection.credential_identity
            key = read_key(
                "openai_compatible" if name == "ollama" else name, interactive
            )
            if key is None:
                raise click.ClickException(
                    "A selected provider needs a credential on hidden input or stdin. Nothing was installed."
                )
            credentials[identity] = key
        if not yes:
            click.echo(
                f"Workspace: {plan.workspace}\nAction: {plan.action}\nComponents: {', '.join(plan.components)}"
            )
            for notice in HandlerSetupPlugins(onex_home=onex_home).consent_notices(
                plan.components
            ):
                click.echo(notice)
            for selection in plan.providers:
                click.echo(
                    f"Models: {selection.provider}: {', '.join(selection.models)}; price: unknown"
                )
            if not click.confirm(
                "Apply this setup and run its selected model proofs, accepting any provider API charges?",
                default=False,
            ):
                raise click.Abort()
    journal = workflow_factory(onex_home=onex_home, credentials=credentials).handle(
        plan
    )
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
