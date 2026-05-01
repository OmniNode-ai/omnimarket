"""ONEX market CLI extension for installed and available node packages."""

from __future__ import annotations

import contextlib
import io
import json
from importlib import resources
from pathlib import Path
from typing import Any

import click
import yaml
from pydantic import BaseModel, ConfigDict, Field

from omnimarket.models.model_metadata import MetadataCapabilities, MetadataSchema


class ModelInstalledMarketPackage(BaseModel):
    """Installed package summary for `onex market list`."""

    model_config = ConfigDict(extra="forbid")

    name: str
    package: str
    version: str | None = None
    source: str | None = None
    path: str | None = None
    display_name: str | None = None
    description: str | None = None
    tags: list[str] = Field(default_factory=list)
    capabilities: MetadataCapabilities = Field(default_factory=MetadataCapabilities)


class ModelMarketIndexEntry(BaseModel):
    """Searchable market index entry."""

    model_config = ConfigDict(extra="forbid")

    name: str
    package: str
    version: str
    description: str
    tags: list[str] = Field(default_factory=list)
    capabilities: MetadataCapabilities = Field(default_factory=MetadataCapabilities)


def _installed_nodes_registry_path() -> Path:
    return Path.home() / ".omnibase" / "installed_nodes.json"


def _load_json_mapping(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        msg = f"Installed node registry must be a JSON object: {path}"
        raise click.ClickException(msg)
    return raw


def _load_node_metadata(path: Path | None) -> MetadataSchema | None:
    if path is None:
        return None
    metadata_path = path / "metadata.yaml"
    if not metadata_path.exists():
        return None
    raw = yaml.safe_load(metadata_path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        msg = f"metadata.yaml must be a mapping: {metadata_path}"
        raise click.ClickException(msg)
    return MetadataSchema.model_validate(raw)


def _load_installed_packages(registry_path: Path) -> list[ModelInstalledMarketPackage]:
    registry = _load_json_mapping(registry_path)
    packages: list[ModelInstalledMarketPackage] = []
    for registry_name, raw_info in sorted(registry.items()):
        if not isinstance(raw_info, dict):
            continue
        package_path = raw_info.get("path")
        metadata = _load_node_metadata(Path(package_path)) if package_path else None
        package_name = str(raw_info.get("package") or registry_name)
        raw_version = raw_info.get("version")
        metadata_version = metadata.version if metadata else None
        version = (
            str(raw_version or metadata_version)
            if raw_version or metadata_version
            else None
        )
        packages.append(
            ModelInstalledMarketPackage(
                name=metadata.name if metadata else str(registry_name),
                package=package_name,
                version=version,
                source=str(raw_info.get("source")) if raw_info.get("source") else None,
                path=str(package_path) if package_path else None,
                display_name=metadata.display_name if metadata else None,
                description=metadata.description if metadata else None,
                tags=metadata.tags if metadata else [],
                capabilities=metadata.capabilities
                if metadata
                else MetadataCapabilities(),
            )
        )
    return packages


def _default_index_path() -> Path:
    return Path(str(resources.files("omnimarket.registry").joinpath("index.yaml")))


def _load_index(index_path: Path) -> list[ModelMarketIndexEntry]:
    if not index_path.exists():
        msg = f"Market index not found: {index_path}"
        raise click.ClickException(msg)
    raw = yaml.safe_load(index_path.read_text(encoding="utf-8")) or {}
    entries = raw.get("packages") if isinstance(raw, dict) else None
    if not isinstance(entries, list):
        msg = f"Market index must contain a packages list: {index_path}"
        raise click.ClickException(msg)
    return [
        ModelMarketIndexEntry.model_validate(entry)
        for entry in entries
        if isinstance(entry, dict)
    ]


def _matches_query(entry: ModelMarketIndexEntry, query: str) -> bool:
    haystack = " ".join(
        [
            entry.name,
            entry.package,
            entry.description,
            *entry.tags,
        ]
    ).lower()
    return query.lower() in haystack


def _capability_flags(capabilities: MetadataCapabilities) -> str:
    flags = [
        "standalone" if capabilities.standalone else "",
        "runtime" if capabilities.full_runtime else "",
        "network" if capabilities.requires_network else "",
        "repo" if capabilities.requires_repo else "",
        "secrets" if capabilities.requires_secrets else "",
        "docker" if capabilities.requires_docker else "",
        capabilities.side_effect_class,
    ]
    return ",".join(flag for flag in flags if flag)


def _echo_json(payload: object) -> None:
    click.echo(json.dumps(payload, indent=2, sort_keys=True))


def _invoke_core_install(
    ctx: click.Context,
    *,
    package_path: str,
    test: bool,
    dry_run: bool,
    upgrade: bool,
    allow_unsigned: bool,
    verbose: bool,
) -> None:
    from omnibase_core.cli.cli_install import cli_install

    ctx.invoke(
        cli_install,
        package_path=package_path,
        test=test,
        dry_run=dry_run,
        upgrade=upgrade,
        allow_unsigned=allow_unsigned,
        verbose=verbose,
    )


@click.group("market")
def market() -> None:
    """Discover and install ONEX market packages."""
    _ = click.get_current_context(silent=True)


@market.command("list")
@click.option("--json", "json_output", is_flag=True, help="Emit machine JSON.")
@click.option(
    "--registry-path",
    type=click.Path(path_type=Path),
    default=None,
    help="Installed node registry path.",
)
def list_installed(json_output: bool, registry_path: Path | None) -> None:
    """List installed ONEX market packages."""

    path = registry_path or _installed_nodes_registry_path()
    packages = _load_installed_packages(path)
    if json_output:
        _echo_json({"packages": [item.model_dump(mode="json") for item in packages]})
        return

    if not packages:
        click.echo("No ONEX market packages installed.")
        return
    for item in packages:
        label = item.display_name or item.name
        version = f" v{item.version}" if item.version else ""
        click.echo(f"{item.name}{version} - {label}")
        click.echo(f"  package: {item.package}")
        click.echo(f"  capabilities: {_capability_flags(item.capabilities)}")


@market.command("install")
@click.argument("package_path")
@click.option(
    "--test/--no-test",
    default=False,
    help="Run golden chain test after install.",
)
@click.option("--dry-run", is_flag=True, help="Analyze package without installing.")
@click.option("--upgrade", is_flag=True, help="Allow replacing installed packages.")
@click.option("--allow-unsigned", is_flag=True, help="Allow unsigned pip packages.")
@click.option("--verbose", "-v", is_flag=True, help="Show delegated install output.")
@click.option("--json", "json_output", is_flag=True, help="Emit machine JSON.")
@click.pass_context
def install_package(
    ctx: click.Context,
    package_path: str,
    test: bool,
    dry_run: bool,
    upgrade: bool,
    allow_unsigned: bool,
    verbose: bool,
    json_output: bool,
) -> None:
    """Install a package by delegating to `onex install`."""

    if not json_output:
        _invoke_core_install(
            ctx,
            package_path=package_path,
            test=test,
            dry_run=dry_run,
            upgrade=upgrade,
            allow_unsigned=allow_unsigned,
            verbose=verbose,
        )
        return

    captured = io.StringIO()
    with contextlib.redirect_stdout(captured):
        _invoke_core_install(
            ctx,
            package_path=package_path,
            test=test,
            dry_run=dry_run,
            upgrade=upgrade,
            allow_unsigned=allow_unsigned,
            verbose=verbose,
        )
    _echo_json(
        {
            "status": "ok",
            "delegated_to": "onex install",
            "package_path": package_path,
            "dry_run": dry_run,
            "output": captured.getvalue().splitlines(),
        }
    )


@market.command("search")
@click.argument("query")
@click.option("--json", "json_output", is_flag=True, help="Emit machine JSON.")
@click.option(
    "--index-path",
    type=click.Path(path_type=Path),
    default=None,
    help="Market index YAML path.",
)
def search(query: str, json_output: bool, index_path: Path | None) -> None:
    """Search the packaged ONEX market index."""

    entries = _load_index(index_path or _default_index_path())
    matches = [entry for entry in entries if _matches_query(entry, query)]
    if json_output:
        _echo_json({"matches": [item.model_dump(mode="json") for item in matches]})
        return

    if not matches:
        click.echo(f"No market packages matched {query!r}.")
        return
    for item in matches:
        click.echo(f"{item.name} v{item.version} - {item.description}")
        click.echo(f"  package: {item.package}")
        click.echo(f"  tags: {', '.join(item.tags)}")
        click.echo(f"  capabilities: {_capability_flags(item.capabilities)}")
