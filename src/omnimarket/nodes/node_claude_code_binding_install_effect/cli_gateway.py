# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The ONEX CLI's explicit Claude gateway binding and credential helper."""

from __future__ import annotations

import asyncio
import json
import os
import shlex
import subprocess
import sys
from pathlib import Path

import click
from omnibase_core.cli.cli_user_config import user_config_path
from pydantic import ValidationError

from omnimarket.inference.local_byok_credential_adapter import LocalByokCredentialStore
from omnimarket.models.delegation.model_claude_gateway import ModelClaudeGatewayConfig
from omnimarket.nodes.node_claude_code_binding_install_effect.handlers.gateway_probe import (
    probe_gateway,
)
from omnimarket.nodes.node_claude_code_binding_install_effect.handlers.handler_claude_gateway_settings import (
    HandlerClaudeGatewaySettings,
)
from omnimarket.nodes.node_claude_code_binding_install_effect.models.model_claude_gateway_settings import (
    ModelClaudeGatewaySettingsRequest,
    ModelClaudeGatewaySettingsResult,
)


def gateway_store() -> LocalByokCredentialStore:
    return LocalByokCredentialStore()


def build_gateway_settings_handler() -> HandlerClaudeGatewaySettings:
    return HandlerClaudeGatewaySettings(
        onex_home=user_config_path().parent,
        claude_home=Path(
            os.environ.get("CLAUDE_CONFIG_DIR", str(Path.home() / ".claude"))
        ),
        probe=probe_gateway,
        working_directory=Path.cwd(),
    )


def _report(result: ModelClaudeGatewaySettingsResult, *, json_output: bool) -> None:
    if json_output:
        click.echo(result.model_dump_json())
    else:
        click.echo(
            f"Claude gateway: {result.status}; {'enabled' if result.enabled else 'disabled'}"
        )
        if result.gateway_url is not None:
            click.echo(f"Gateway: {result.gateway_url}")
        if result.model is not None:
            click.echo(f"Model: {result.model}")
        if result.changed:
            click.echo("Restart existing Claude Code sessions to apply this change.")
        if result.conflicts:
            click.echo("Conflicts: " + ", ".join(result.conflicts), err=True)
        if result.fix:
            click.echo(result.fix, err=True)
    if result.status != "success":
        raise click.exceptions.Exit(1)


def _consent(yes: bool, json_output: bool, prompt: str) -> None:
    if yes:
        return
    if json_output:
        _report(
            ModelClaudeGatewaySettingsResult(
                status="action_required",
                fix="Review the gateway destination, model and API billing, then repeat with --yes.",
            ),
            json_output=True,
        )
    if not click.confirm(prompt, default=False):
        raise click.Abort()


@click.group("claude")
def claude_group() -> None:  # stub-ok: Click group
    """Route Claude Code model requests through an ONEX gateway."""


@claude_group.group("hooks")
def gateway_hooks_group() -> None:  # stub-ok: Click group
    """Install or inspect the optional gateway guards for Claude Code."""


def _hooks_action(action: str, *, yes: bool, json_output: bool) -> None:
    from omnimarket.nodes.node_claude_code_binding_install_effect.handlers.handler_gateway_hook_install import (
        HandlerGatewayHookInstall,
    )
    from omnimarket.nodes.node_claude_code_binding_install_effect.models.model_gateway_hook_install import (
        ModelGatewayHookInstallRequest,
        ModelGatewayHookInstallResult,
    )

    if action != "status" and not yes:
        if json_output or not sys.stdin.isatty():
            result = ModelGatewayHookInstallResult(
                status="action_required",
                fix="Review the gateway hook registration change, then repeat with --yes.",
            )
        elif not click.confirm(
            "Change ONEX gateway guard registrations in your Claude settings?",
            default=False,
        ):
            raise click.Abort()
        else:
            result = HandlerGatewayHookInstall().handle(
                ModelGatewayHookInstallRequest.model_validate({"action": action})
            )
    else:
        result = HandlerGatewayHookInstall().handle(
            ModelGatewayHookInstallRequest.model_validate({"action": action})
        )
    if json_output:
        click.echo(result.model_dump_json())
    else:
        click.echo(
            f"Claude gateway hooks: {result.status}; {'registered' if result.registered else 'not registered'}"
        )
    if result.fix:
        click.echo(result.fix, err=True)
    if result.status != "success":
        raise click.exceptions.Exit(1)


@gateway_hooks_group.command("install")
@click.option("--yes", is_flag=True)
@click.option("--json", "json_output", is_flag=True)
def gateway_hooks_install(yes: bool, json_output: bool) -> None:
    """Register the contract's gateway checks while preserving unrelated settings."""
    _hooks_action("install", yes=yes, json_output=json_output)


@gateway_hooks_group.command("status")
@click.option("--json", "json_output", is_flag=True)
def gateway_hooks_status(json_output: bool) -> None:
    """Observe registered gateway checks without writing settings."""
    _hooks_action("status", yes=False, json_output=json_output)


@gateway_hooks_group.command("uninstall")
@click.option("--yes", is_flag=True)
@click.option("--json", "json_output", is_flag=True)
def gateway_hooks_uninstall(yes: bool, json_output: bool) -> None:
    """Remove this install's exact hook commands and preserve later user edits."""
    _hooks_action("uninstall", yes=yes, json_output=json_output)


@claude_group.command("hook", hidden=True)
def hook_command() -> None:
    """Check a gateway binding at a Claude hook boundary, without inference."""
    from omnimarket.models.delegation.model_claude_gateway_hook import (
        ModelClaudeGatewayHookRequest,
    )
    from omnimarket.nodes.node_claude_code_hook_adapter_effect.handlers.handler_claude_gateway_hook import (
        HandlerClaudeGatewayHook,
    )

    try:
        raw = sys.stdin.read(1024 * 1024 + 1)
        if len(raw.encode()) > 1024 * 1024:
            raise ValueError("Hook metadata too large")
        request = ModelClaudeGatewayHookRequest.model_validate(json.loads(raw))
        result = HandlerClaudeGatewayHook().handle(request)
    except (OSError, ValueError):
        click.echo(
            "ONEX gateway guard could not validate hook metadata. Run onex claude status.",
            err=True,
        )
        raise click.exceptions.Exit(2) from None
    if result.decision == "block":
        click.echo(
            result.reason or "Restore the ONEX gateway before retrying.", err=True
        )
        raise click.exceptions.Exit(2)


@claude_group.command("gateway")
@click.option(
    "--backend-id", required=True, help="Exact native Messages backend in ONEX routing."
)
@click.option(
    "--model", required=True, help="Exact model declared by the selected backend."
)
@click.option(
    "--credential-ref",
    required=True,
    help="Gateway client credential reference in the ONEX store.",
)
@click.option(
    "--host", default="127.0.0.1", type=click.Choice(["127.0.0.1", "::1", "localhost"])
)
@click.option("--port", default=8181, type=click.IntRange(1, 65535))
def gateway_command(
    backend_id: str, model: str, credential_ref: str, host: str, port: int
) -> None:
    """Serve an authenticated local gateway using ONEX routing and evidence."""
    from omnimarket.handlers.claude_gateway import serve_local_gateway

    try:
        config = ModelClaudeGatewayConfig(
            backend_id=backend_id,
            model=model,
            credential_ref=credential_ref,
            bind_host=host,
            bind_port=port,
        )
    except ValidationError:
        raise click.ClickException(
            "Provide a valid native backend, model and gateway credential reference."
        ) from None
    shown_host = f"[{host}]" if host == "::1" else host
    click.echo(
        f"Gateway: http://{shown_host}:{port}\nBackend: {backend_id}\nModel: {model}\nPrice: unknown"
    )
    try:
        serve_local_gateway(config)
    except (OSError, ValueError):
        raise click.ClickException(
            "The local gateway could not start; check onex local identity, routing and the selected port."
        ) from None


@claude_group.command("enable")
@click.option(
    "--gateway-url",
    required=True,
    help="The ONEX gateway base URL (HTTPS or loopback HTTP).",
)
@click.option(
    "--model", required=True, help="A Claude-compatible model served by this gateway."
)
@click.option(
    "--credential-ref",
    required=True,
    help="Gateway credential reference in the ONEX secret store.",
)
@click.option(
    "--yes", is_flag=True, help="Accept the gateway destination, model and API billing."
)
@click.option("--json", "json_output", is_flag=True)
def enable_command(
    gateway_url: str, model: str, credential_ref: str, yes: bool, json_output: bool
) -> None:
    """Enable gateway routing for newly launched Claude Code sessions."""
    helper = shlex.join(
        [
            str(Path(sys.executable).absolute()),
            "-m",
            "omnimarket.nodes.node_claude_code_binding_install_effect.cli_gateway",
            "credential",
            "--ref",
            credential_ref,
        ]
    )
    try:
        request = ModelClaudeGatewaySettingsRequest(
            action="enable",
            gateway_url=gateway_url,
            model=model,
            credential_ref=credential_ref,
            credential_helper=helper,
            environment=dict(os.environ),
        )
    except ValidationError:
        _report(
            ModelClaudeGatewaySettingsResult(
                status="failure",
                fix="Provide a safe gateway URL, single-line model and public credential reference.",
            ),
            json_output=json_output,
        )
        return
    if not json_output:
        click.echo(
            f"Gateway: {request.gateway_url}\nModel: {request.model}\nCost: unknown; gateway API billing may replace Claude subscription billing."
        )
    _consent(yes, json_output, "Enable this gateway for Claude Code?")
    _report(build_gateway_settings_handler().handle(request), json_output=json_output)


@claude_group.command("status")
@click.option("--json", "json_output", is_flag=True)
def status_command(json_output: bool) -> None:
    """Observe the current binding and authenticated gateway connectivity."""
    _report(
        build_gateway_settings_handler().handle(
            ModelClaudeGatewaySettingsRequest(
                action="status", environment=dict(os.environ)
            )
        ),
        json_output=json_output,
    )


@claude_group.command("disable")
@click.option("--yes", is_flag=True)
@click.option("--json", "json_output", is_flag=True)
def disable_command(yes: bool, json_output: bool) -> None:
    """Remove still-owned ONEX settings while retaining user edits."""
    _consent(
        yes, json_output, "Disable the ONEX gateway binding for new Claude sessions?"
    )
    _report(
        build_gateway_settings_handler().handle(
            ModelClaudeGatewaySettingsRequest(
                action="disable", environment=dict(os.environ)
            )
        ),
        json_output=json_output,
    )


@claude_group.command("credential", hidden=True)
@click.option("--ref", "reference", required=True)
def credential_command(reference: str) -> None:
    """Claude apiKeyHelper: print only the credential resolved from its reference."""
    try:
        ModelClaudeGatewaySettingsRequest(action="status", credential_ref=reference)
        value = asyncio.run(gateway_store().get_secret(reference))
        if not value or any(ord(c) < 32 or ord(c) == 127 for c in value):
            raise ValueError("Missing or invalid credential")
    except Exception:
        raise click.ClickException(
            "Gateway credential unavailable; repair the ONEX secret-store reference."
        ) from None
    click.echo(value)


@claude_group.command("start", context_settings={"ignore_unknown_options": True})
@click.argument("args", nargs=-1, type=click.UNPROCESSED)
@click.pass_context
def start_command(ctx: click.Context, args: tuple[str, ...]) -> None:
    """Launch Claude after verifying the gateway binding; pass Claude flags after --."""
    if any(
        arg.split("=", 1)[0]
        in (
            "--settings",
            "--setting-sources",
            "--env",
            "--bare",
            "--restricted",
            "--cloud",
            "--desktop",
            "--environment",
            "--teleport",
        )
        for arg in args
    ):
        raise click.ClickException(
            "Routing override arguments are not permitted in gateway mode."
        )
    result = build_gateway_settings_handler().handle(
        ModelClaudeGatewaySettingsRequest(action="status", environment=dict(os.environ))
    )
    if (
        result.status != "success"
        or not result.enabled
        or not result.gateway_url
        or not result.model
    ):
        raise click.ClickException(
            "Gateway routing is unavailable; run onex claude status before launching Claude."
        )
    try:
        process = subprocess.run(["claude", *args], check=False)
    except OSError:
        raise click.ClickException(
            "Claude Code could not be launched; check its installation."
        ) from None
    ctx.exit(process.returncode)


if __name__ == "__main__":
    claude_group()
