# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""``onex settings set|get|list|bootstrap-file``: one declared command over the central settings store.

A shim over ``node_overlay_setting_set_effect`` (the set), the injected store of ``store_wiring`` (get and list) and
``render_bootstrap_file`` (the host's bootstrap file); it holds no store address, kind or setting of its own. The store a
read uses is built from the host's bootstrap file (``--bootstrap-file`` or ``ONEX_BOOTSTRAP_FILE``) through the one wiring
function, and a secret that kind declares is taken from the environment variable of the same name, which the host's secret
injection fills.

A set that succeeded in the store also rewrites that overlay's local file (``<root>/<overlay>/overlay.yaml`` under
``ONEX_SKILL_OVERLAY_ROOTS``) with the new value and a ``# settings-set:`` provenance line (path, actor, time and the value's
digest, never the value), so the next bootstrap seeds the latest value.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from collections.abc import Mapping
from datetime import UTC, datetime
from importlib import resources
from pathlib import Path
from typing import Any

import click
import yaml
from omnibase_spi.protocols.storage import ProtocolOverlayStore
from pydantic import JsonValue

from omnimarket.handlers.node_overlay_reader import (
    OVERLAY_ROOTS_ENV,
    find_node_overlay,
)
from omnimarket.handlers.overlay_store.bootstrap_file import (
    BootstrapInputMissingError,
    parse_bootstrap_file,
    render_bootstrap_file,
)
from omnimarket.handlers.overlay_store.setting_paths import (
    MISSING,
    get_at,
    set_at,
    value_digest,
)
from omnimarket.handlers.overlay_store.store_wiring import build_overlay_store
from omnimarket.nodes.node_overlay_setting_set_effect.handlers.handler_overlay_setting_set import (
    load_settable_settings,
)
from omnimarket.nodes.node_overlay_setting_set_effect.models.model_settable_setting import (
    ModelSettableSetting,
)
from omnimarket.nodes.node_overlay_setting_set_effect.runtime_overlay_setting_set import (
    main as _set_main,
)

BOOTSTRAP_FILE_ENV = "ONEX_BOOTSTRAP_FILE"
ACTOR_ENV = "ONEX_ACTOR"
PROVENANCE_PREFIX = "# settings-set:"
UNSET = "<unset>"
_SET_NODE = "node_overlay_setting_set_effect"


def _declared() -> tuple[ModelSettableSetting, ...]:
    text = (
        resources.files(f"omnimarket.nodes.{_SET_NODE}")
        .joinpath("contract.yaml")
        .read_text(encoding="utf-8")
    )
    return load_settable_settings(yaml.safe_load(text))


def _resolve(overlay: str, key: str) -> tuple[ModelSettableSetting, str]:
    for setting in _declared():
        if setting.overlay_id == overlay:
            path = setting.resolve(key)
            if path is not None:
                return setting, path
    raise click.ClickException(
        f"{key} is not a key the contract declares settable for the overlay {overlay}"
    )


def _env_secrets(names: tuple[str, ...]) -> Mapping[str, str]:
    return {name: os.environ.get(name, "") for name in names}


def _store(ctx: click.Context, bootstrap_file: Path | None) -> ProtocolOverlayStore:
    injected = (ctx.obj or {}).get("store")
    if injected is not None:
        store: ProtocolOverlayStore = injected
        return store
    path = bootstrap_file or (
        Path(os.environ[BOOTSTRAP_FILE_ENV])
        if os.environ.get(BOOTSTRAP_FILE_ENV)
        else None
    )
    if path is None:
        raise click.ClickException(
            f"name the host's bootstrap file with --bootstrap-file or {BOOTSTRAP_FILE_ENV}"
        )
    try:
        bootstrap = parse_bootstrap_file(path.read_text(encoding="utf-8"))
        return build_overlay_store(bootstrap, fetch_secrets=_env_secrets)
    except (OSError, ValueError) as exc:
        raise click.ClickException(f"{type(exc).__name__}: {exc}") from exc


async def _read(store: ProtocolOverlayStore, overlay: str) -> Mapping[str, Any] | None:
    return await store.read_current(overlay)


def _current(store: ProtocolOverlayStore, path: str, overlay: str) -> JsonValue | None:
    """The stored value at ``path``, or ``None`` when the store holds no record or the path holds nothing."""
    record = asyncio.run(_read(store, overlay))
    if record is None:
        return None
    document = record.get("document")
    if not isinstance(document, dict):
        return None
    found = get_at(document, path)
    return None if found is MISSING else found  # type: ignore[return-value]


bootstrap_option = click.option(
    "--bootstrap-file",
    type=click.Path(dir_okay=False, path_type=Path),
    default=None,
    help=f"the host's bootstrap file (default: {BOOTSTRAP_FILE_ENV})",
)


@click.group("settings")
@click.pass_context
def settings_group(ctx: click.Context) -> None:
    """Set, read and list the settings the central store holds."""
    ctx.ensure_object(dict)


@settings_group.command("get")
@click.argument("overlay")
@click.argument("key")
@bootstrap_option
@click.pass_context
def get_command(
    ctx: click.Context, overlay: str, key: str, bootstrap_file: Path | None
) -> None:
    """Print one declared setting's stored value as JSON."""
    _setting, path = _resolve(overlay, key)
    store = _store(ctx, bootstrap_file)
    value = _current(store, path, overlay)
    if value is None:
        raise click.ClickException(
            f"the store holds no value of {key} for the overlay {overlay}"
        )
    click.echo(json.dumps(value))


@settings_group.command("list")
@click.option("--overlay", default=None, help="only this overlay's declared settings")
@bootstrap_option
@click.pass_context
def list_command(
    ctx: click.Context, overlay: str | None, bootstrap_file: Path | None
) -> None:
    """List each declared setting, tab-separated: overlay, key, current value (or <unset>)."""
    store = _store(ctx, bootstrap_file)
    for setting in _declared():
        if overlay is not None and setting.overlay_id != overlay:
            continue
        value: JsonValue | None = None
        if "{" not in setting.path:  # a placeholder key has no one value to show
            value = _current(store, setting.path, setting.overlay_id)
        shown = UNSET if value is None else json.dumps(value)
        click.echo(f"{setting.overlay_id}\t{setting.key}\t{shown}")


@settings_group.command("set")
@click.argument("overlay")
@click.argument("key")
@click.argument("value_json")
@click.option(
    "--actor",
    envvar=ACTOR_ENV,
    required=True,
    help="who sets it: the audit row names it",
)
def set_command(overlay: str, key: str, value_json: str, actor: str) -> None:
    """Set one declared setting in the store, with an audit row, then rewrite the overlay's local file."""
    setting, path = _resolve(overlay, key)
    try:
        value = json.loads(value_json)
    except json.JSONDecodeError as exc:
        raise click.ClickException(f"the value is not JSON: {exc}") from exc
    code = _set_main(
        [
            "--overlay",
            overlay,
            "--key",
            key,
            "--actor",
            actor,
            "--value-json",
            value_json,
        ]
    )
    if code != 0:
        sys.exit(code)
    rewritten = write_back_overlay_file(
        setting, path, value, actor, datetime.now(UTC), os.environ
    )
    if rewritten is None:
        click.echo(
            f"no local overlay file for {overlay} under {OVERLAY_ROOTS_ENV}; "
            "the store holds the new value, nothing was rewritten"
        )
    else:
        click.echo(f"rewrote {rewritten}")


def write_back_overlay_file(
    setting: ModelSettableSetting,
    path: str,
    value: JsonValue,
    actor: str,
    now: datetime,
    environ: Mapping[str, str],
) -> Path | None:
    """Put ``value`` into the overlay's local file with one more provenance line; ``None`` when no file is under the roots."""
    file = find_node_overlay(setting.overlay_id, environ)
    if file is None:
        return None
    text = file.read_text(encoding="utf-8")
    provenance = [
        line for line in text.splitlines() if line.startswith(PROVENANCE_PREFIX)
    ]
    document = yaml.safe_load(text)
    if not isinstance(document, dict):
        raise click.ClickException(f"{file} is not a mapping")
    if setting.whole_document:
        if not isinstance(value, dict):
            raise click.ClickException("a whole-document setting takes a mapping")
        document.update(value)
        marked = ",".join(sorted(value))
    else:
        set_at(document, path, value)
        marked = path
    provenance.append(
        f"{PROVENANCE_PREFIX} path={marked} actor={actor} at={now.isoformat()} digest={value_digest(value)}"
    )
    body = yaml.safe_dump(document, sort_keys=False)
    file.write_text("\n".join(provenance) + "\n" + body, encoding="utf-8")
    return file


@settings_group.command("bootstrap-file")
@click.option(
    "--intake",
    required=True,
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
)
@click.option("--out", required=True, type=click.Path(dir_okay=False, path_type=Path))
def bootstrap_file_command(intake: Path, out: Path) -> None:
    """Render a host's bootstrap file from the intake file: the binding names only, never a setting."""
    values: dict[str, str] = {}
    for raw in intake.read_text(encoding="utf-8").splitlines():
        name, separator, value = raw.partition("=")
        if separator and not raw.lstrip().startswith("#"):
            values[name.strip()] = value.strip()
    try:
        rendered = render_bootstrap_file(values)
    except BootstrapInputMissingError as exc:
        raise click.ClickException(str(exc)) from exc
    out.write_text(rendered, encoding="utf-8")
    click.echo(f"wrote {out}")


__all__ = ["settings_group", "write_back_overlay_file"]
