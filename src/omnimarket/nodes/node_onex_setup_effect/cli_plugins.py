# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Manage installed capability manifests and their real lifecycle contributions."""

from __future__ import annotations

import json
import sys
from importlib import import_module, invalidate_caches
from importlib.metadata import PackageNotFoundError, distribution
from pathlib import Path

import click
from packaging.requirements import InvalidRequirement, Requirement
from packaging.utils import (
    InvalidWheelFilename,
    canonicalize_name,
    parse_wheel_filename,
)
from pydantic import ValidationError

from omnimarket.nodes.node_onex_setup_effect.handlers.handler_setup_plugin_lifecycle import (
    HandlerSetupPluginLifecycle,
)
from omnimarket.nodes.node_onex_setup_effect.handlers.handler_setup_plugins import (
    HandlerSetupPlugins,
)
from omnimarket.nodes.node_onex_setup_effect.models.model_setup_plugin import (
    ModelSetupPluginAction,
)


def _registry() -> HandlerSetupPlugins:
    try:
        registry = HandlerSetupPlugins()
        registry.manifests()
        return registry
    except Exception:
        raise click.ClickException(
            "Plugin manifests or configuration are invalid; inspect the installed package and setup_plugins configuration."
        ) from None


def _transition(name: str, action: str, json_output: bool) -> None:
    try:
        result = HandlerSetupPluginLifecycle(registry=_registry()).handle(
            ModelSetupPluginAction(plugin=name, action=action)
        )
    except ValidationError:
        raise click.ClickException("Invalid plugin lifecycle selection.") from None
    except (OSError, ValueError) as error:
        # Registry and callback failures contain only controlled diagnostics.
        raise click.ClickException(str(error)) from None
    click.echo(
        result.model_dump_json()
        if json_output
        else f"{name}: {'enabled' if result.enabled else 'disabled'} ({'changed' if result.changed else 'unchanged'})"
    )


class _PluginCommands(click.Group):
    def list_commands(self, ctx: click.Context) -> list[str]:
        registry = _registry()
        return sorted(
            p.id for p in registry.manifests() if registry.is_enabled(p) and p.commands
        )

    def get_command(self, ctx: click.Context, name: str) -> click.Command | None:
        registry = _registry()
        try:
            plugin = registry.plugin(name)
        except ValueError:
            return None
        if not registry.is_enabled(plugin):
            return None
        group = click.Group(name=name)
        for command_name, reference in plugin.commands.items():
            module, attribute = reference.split(":")
            command = getattr(import_module(module), attribute)
            if not isinstance(command, click.Command):
                raise click.ClickException(
                    f"Invalid command contribution: {name}/{command_name}"
                )
            group.add_command(command, command_name)
        return group


plugins_group = click.Group(
    "plugins",
    help="Manage ONEX capability plugins; Claude bundles keep their host's format.",
)


@plugins_group.command("list")
@click.option("--json", "json_output", is_flag=True)
def list_plugins(json_output: bool) -> None:
    registry = _registry()
    rows = [
        {
            "id": p.id,
            "version": p.version,
            "enabled": registry.is_enabled(p),
            "package": registry.packages.get(p.id),
            "components": p.components,
        }
        for p in registry.manifests()
    ]
    if json_output:
        click.echo(json.dumps(rows))
    else:
        for row in rows:
            click.echo(
                f"{row['id']} {row['version']} {'enabled' if row['enabled'] else 'disabled'}"
            )


@plugins_group.command("info")
@click.argument("name")
def plugin_info(name: str) -> None:
    registry = _registry()
    try:
        plugin = registry.plugin(name)
    except ValueError as error:
        raise click.ClickException(str(error)) from None
    click.echo(plugin.model_dump_json(indent=2))


@plugins_group.command("enable")
@click.argument("name")
@click.option("--json", "json_output", is_flag=True)
def enable_plugin(name: str, json_output: bool) -> None:
    _transition(name, "enable", json_output)


@plugins_group.command("disable")
@click.argument("name")
@click.option("--json", "json_output", is_flag=True)
def disable_plugin(name: str, json_output: bool) -> None:
    _transition(name, "disable", json_output)


def _install(
    ctx: click.Context, package: str, upgrade: bool, dry_run: bool, allow_unsigned: bool
) -> None:
    from omnibase_core.cli.cli_install import cli_install

    try:
        package_name = (
            str(parse_wheel_filename(Path(package).name)[0])
            if package.endswith(".whl")
            else Requirement(package).name
        )
    except (InvalidRequirement, InvalidWheelFilename):
        raise click.ClickException(
            "Name a Python capability package or wheel; source checkouts and other host plugin formats use their own installers."
        ) from None

    ctx.invoke(
        cli_install,
        package_path=package,
        test=False,
        dry_run=dry_run,
        upgrade=upgrade,
        allow_unsigned=allow_unsigned,
        verbose=False,
    )
    if not dry_run:
        invalidate_caches()
        registry = _registry()
        owned = [
            name
            for name, owner in registry.packages.items()
            if canonicalize_name(owner) == canonicalize_name(package_name)
        ]
        if not owned:
            raise click.ClickException(
                "The installed package declares no onex.setup capability. It was installed, but no ONEX plugin was enabled; inspect or remove it with onex uninstall."
            )
        click.echo(
            "Plugin registry validated. Review onex plugins list and enable the desired capability."
        )


@plugins_group.command("install")
@click.argument("package")
@click.option("--dry-run", is_flag=True)
@click.option("--allow-unsigned", is_flag=True)
@click.pass_context
def install_plugin(
    ctx: click.Context, package: str, dry_run: bool, allow_unsigned: bool
) -> None:
    """Install a versioned package through the canonical contract package installer."""
    _install(ctx, package, False, dry_run, allow_unsigned)


@plugins_group.command("update")
@click.argument("package")
@click.option("--dry-run", is_flag=True)
@click.option("--allow-unsigned", is_flag=True)
@click.pass_context
def update_plugin(
    ctx: click.Context, package: str, dry_run: bool, allow_unsigned: bool
) -> None:
    """Install an explicitly selected package version using the existing upgrade owner."""
    _install(ctx, package, True, dry_run, allow_unsigned)


@plugins_group.command("uninstall")
@click.argument("name")
@click.option("--yes", is_flag=True)
@click.pass_context
def uninstall_plugin(ctx: click.Context, name: str, yes: bool) -> None:
    """Dispose an external plugin before removing its owning package."""
    from omnibase_core.cli.cli_install import cli_uninstall

    registry = _registry()
    package = registry.packages.get(name)
    if package is None or package.lower().replace("_", "-") == "omnimarket":
        raise click.ClickException(
            "Built-in capabilities cannot be uninstalled separately; use disable."
        )
    siblings = [
        p.id
        for p in registry.manifests()
        if p.id != name and registry.packages.get(p.id) == package
    ]
    if siblings:
        raise click.ClickException(
            "The package also owns plugins: "
            + ", ".join(siblings)
            + ". Remove the package deliberately with onex uninstall after disabling its capabilities."
        )
    if not yes and not click.confirm(
        f"Disable {name} and uninstall {package}?", default=False
    ):
        raise click.Abort()
    _transition(name, "disable", False)
    ctx.invoke(cli_uninstall, package_name=package)
    invalidate_caches()
    try:
        distribution(package)
    except PackageNotFoundError:
        return
    raise click.ClickException(
        "Package removal failed; plugin remains disabled. Inspect the package installer output."
    )


plugins_group.add_command(
    _PluginCommands(
        name="run", help="Run a command declared by an enabled capability plugin."
    )
)


def _worker(name: str, action: str, home: str) -> None:
    registry = HandlerSetupPlugins(onex_home=Path(home))
    plugin = registry.plugin(name)
    if action not in ("enable", "disable"):
        raise ValueError("Invalid lifecycle action")
    reference = getattr(plugin.lifecycle, action)
    if reference is None:
        return
    module, attribute = reference.split(":")
    callback = getattr(import_module(module), attribute)
    callback(onex_home=Path(home))


if __name__ == "__main__":
    if len(sys.argv) == 5 and sys.argv[1] == "--lifecycle-worker":
        _worker(*sys.argv[2:])
    else:
        plugins_group()
