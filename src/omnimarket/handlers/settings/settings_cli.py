# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""``onex settings set|get|list|bootstrap-file``: one declared command over the central settings store.

A shim over ``node_overlay_setting_set_effect`` (the set), the table (get, list and the seed's agreement check) and
``render_bootstrap_file`` (the host's bootstrap file); it holds no store address, kind or setting of its own. The table is
the settings store of record: the database the projection writes, read through the snapshot topic the projection publishes
after it commits (nothing but a runtime connects to a database). A set publishes the declared loaded event and the store's
reply names the committed row; a get or list reads the table, never a cache (OMN-24028).

A set that succeeded in the store also rewrites that overlay's local file (``<root>/<overlay>/overlay.yaml`` under
``ONEX_SKILL_OVERLAY_ROOTS``) with the new value and a ``# settings-set:`` provenance line (path, actor, time and the value's
digest, never the value), so the next bootstrap seeds the latest value.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from collections.abc import Coroutine, Mapping
from datetime import UTC, datetime
from importlib import resources
from pathlib import Path
from typing import Any, cast

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
from omnimarket.handlers.overlay_store.settings_seed import seed_defaults
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
PROVISION_NODE = "node_settings_store_provision_effect"
PROVISION_BOOTSTRAP_KEY = "bootstrap_file"
SEED_ACTOR = "onex-settings-seed"
SEED_AGREE_WAIT_S = 30.0


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


_LOOP: asyncio.AbstractEventLoop | None = None


def _run_on_one_loop[T](coro: Coroutine[Any, Any, T]) -> T:
    """Run ``coro`` on this process's one event loop: a Valkey client belongs to the loop that first used it, so a
    new loop per read made every read after the first fail (measured on the dev store, OMN-24052)."""
    global _LOOP
    if _LOOP is None or _LOOP.is_closed():
        _LOOP = asyncio.new_event_loop()
    return _LOOP.run_until_complete(coro)


async def _read(store: ProtocolOverlayStore, overlay: str) -> Mapping[str, Any] | None:
    return await store.read_current(overlay)


def _current(store: ProtocolOverlayStore, path: str, overlay: str) -> JsonValue | None:
    """The stored value at ``path``, or ``None`` when the store holds no record or the path holds nothing."""
    record = _run_on_one_loop(_read(store, overlay))
    if record is None:
        return None
    document = record.get("document")
    if not isinstance(document, dict):
        return None
    found = get_at(document, path)
    return None if found is MISSING else cast(JsonValue, found)


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
@click.pass_context
def get_command(ctx: click.Context, overlay: str, key: str) -> None:
    """Print one declared setting's stored value as JSON."""
    _setting, path = _resolve(overlay, key)
    value = _current(_live_table(ctx)[0], path, overlay)
    if value is None:
        raise click.ClickException(
            f"the store holds no value of {key} for the overlay {overlay}"
        )
    click.echo(json.dumps(value))


@settings_group.command("list")
@click.option("--overlay", default=None, help="only this overlay's declared settings")
@click.pass_context
def list_command(ctx: click.Context, overlay: str | None) -> None:
    """List each declared setting, tab-separated: overlay, key, current value (or <unset>)."""
    table = _live_table(ctx)[0]
    for setting in _declared():
        if overlay is not None and setting.overlay_id != overlay:
            continue
        value: JsonValue | None = None
        if "{" not in setting.path:  # a placeholder key has no one value to show
            value = _current(table, setting.path, setting.overlay_id)
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


def _declared_bootstrap_path(environ: Mapping[str, str]) -> Path | None:
    """The bootstrap file path the store provision node's overlay declares (``bootstrap_file``), never a built-in path."""
    file = find_node_overlay(PROVISION_NODE, environ)
    if file is None:
        return None
    document = yaml.safe_load(file.read_text(encoding="utf-8"))
    declared = (
        document.get(PROVISION_BOOTSTRAP_KEY) if isinstance(document, dict) else None
    )
    if not isinstance(declared, str) or not declared.strip():
        return None
    return Path(declared).expanduser()


def _overlay_defaults(
    overlay_ids: list[str], environ: Mapping[str, str]
) -> dict[str, dict[str, JsonValue]]:
    """Each declared overlay's local file as its default document; an overlay with no file is left out."""
    defaults: dict[str, dict[str, JsonValue]] = {}
    for overlay_id in overlay_ids:
        file = find_node_overlay(overlay_id, environ)
        if file is None:
            continue
        document = yaml.safe_load(file.read_text(encoding="utf-8"))
        if isinstance(document, dict) and document:
            defaults[overlay_id] = document
    return defaults


async def _refresh(table: Any) -> None:
    refresh = getattr(table, "refresh", None)
    if refresh is not None:
        await refresh()


def _live_table(ctx: click.Context) -> tuple[ProtocolOverlayStore, Any]:
    """The table reader and its bus opener: injected for a test, the loader's bus and snapshot topic otherwise."""
    injected = (ctx.obj or {}).get("table")
    if injected is not None:
        table: ProtocolOverlayStore = injected
        return table, None
    from omnimarket.handlers.node_overlay_reader import load_structured_overlay
    from omnimarket.handlers.overlay_store.bus_opener import (
        open_lane_bus,
        workspace_root,
    )
    from omnimarket.handlers.overlay_store.store_bus_snapshot import (
        BusSnapshotOverlayStore,
    )
    from omnimarket.models.overlay_store import ModelOverlayStoreLoadOverlay
    from omnimarket.nodes.node_overlay_setting_set_effect.runtime_overlay_setting_set import (
        LOADER_NODE,
        _contract,
    )

    contract = _contract()
    overlay = load_structured_overlay(
        LOADER_NODE, ModelOverlayStoreLoadOverlay, os.environ
    )

    def open_bus() -> Any:
        return open_lane_bus(
            bus=overlay.bus,
            lane=None
            if overlay.kafka_bootstrap or overlay.bus == "inmemory"
            else overlay.bus_lane,
            kafka_bootstrap=overlay.kafka_bootstrap,
            root=workspace_root(os.environ),
        )

    return (
        BusSnapshotOverlayStore(
            open_bus=open_bus,
            topic=contract["snapshot_read"]["topic"],
            group_prefix=contract["config"]["group_prefix"],
            window_s=float(contract["config"]["read_window_s"]),
        ),
        (open_bus, contract["event_bus"]["publish_topics"][0]),
    )


def _live_seed_write(
    ctx: click.Context, table: ProtocolOverlayStore, wiring: Any
) -> Any:
    """The write path of a seed: injected for a test, the loaded event on the loader's bus otherwise, then wait for the
    table to show the record (the projection commits it and publishes the snapshot)."""
    injected = (ctx.obj or {}).get("seed_write")
    if injected is not None:
        return injected
    open_bus, loaded_topic = wiring

    async def write(record: Mapping[str, JsonValue]) -> None:
        async with open_bus() as bus:
            await bus.publish(
                loaded_topic,
                str(record["overlay_id"]).encode("utf-8"),
                json.dumps(record).encode("utf-8"),
            )
        loop = asyncio.get_running_loop()
        deadline = loop.time() + SEED_AGREE_WAIT_S
        while loop.time() < deadline:
            await _refresh(table)
            current = await table.read_current(str(record["overlay_id"]))
            if current is not None and current.get("digest") == record["digest"]:
                return
            await asyncio.sleep(1.0)

    return write


@settings_group.command("seed")
@click.option(
    "--overlay",
    default=None,
    help="seed only this overlay's default (default: every declared overlay)",
)
@bootstrap_option
@click.pass_context
def seed_command(
    ctx: click.Context, overlay: str | None, bootstrap_file: Path | None
) -> None:
    """Load each declared overlay's default into the store through the seed, never over a live value.

    An overlay the store already holds is kept as it is and reported; a second run writes nothing. The bootstrap file is
    ``--bootstrap-file``, ``ONEX_BOOTSTRAP_FILE`` or the path the store provision node's overlay declares.
    """
    path = bootstrap_file
    if path is None and os.environ.get(BOOTSTRAP_FILE_ENV):
        path = Path(os.environ[BOOTSTRAP_FILE_ENV])
    if path is None:
        path = _declared_bootstrap_path(os.environ)
    if path is None:
        raise click.ClickException(
            f"name the host's bootstrap file with --bootstrap-file, {BOOTSTRAP_FILE_ENV} or the "
            f"{PROVISION_BOOTSTRAP_KEY} value of the {PROVISION_NODE} overlay"
        )
    try:
        intake = parse_bootstrap_file(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise click.ClickException(f"{type(exc).__name__}: {exc}") from exc
    table, wiring = _live_table(ctx)
    ids = sorted({s.overlay_id for s in _declared() if overlay in (None, s.overlay_id)})
    defaults = _overlay_defaults(ids, os.environ)
    kept: list[str] = []
    to_seed: dict[str, dict[str, JsonValue]] = {}
    for overlay_id, document in defaults.items():
        if _run_on_one_loop(_read(table, overlay_id)) is not None:
            kept.append(overlay_id)  # a live value is never overwritten
        else:
            to_seed[overlay_id] = document
    write = _live_seed_write(ctx, table, wiring)
    try:
        result = _run_on_one_loop(
            seed_defaults(
                intake,
                to_seed,
                table=table,
                write=write,
                source_ref="onex-settings-seed",
                loaded_by=os.environ.get(ACTOR_ENV, SEED_ACTOR),
                now=datetime.now(UTC),
            )
        )
    except (ValueError, RuntimeError) as exc:
        raise click.ClickException(f"{type(exc).__name__}: {exc}") from exc
    for overlay_id in result.written:
        click.echo(f"seeded\t{overlay_id}")
    for overlay_id in sorted([*result.unchanged, *kept]):
        click.echo(f"kept\t{overlay_id}")
    for overlay_id in ids:
        if overlay_id not in defaults:
            click.echo(f"no-default\t{overlay_id}")


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
