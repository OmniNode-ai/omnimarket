# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Entry: ``python -m <this module> --overlay-json <J> --this-host <H> <command> [key]``.

This is the node's runtime-module form (the census node's form): the tick unit and the prompt-submit hook entry
both run it with the dispatch venv's interpreter. ``J`` is the overlay's ``floor_fill`` section wrapped as
``{"floor_fill": {...}}``; an empty or unreadable ``J`` prints ``UNKNOWN fill-state:overlay:unreadable`` and fails.

After a live ``tick`` the entry runs one alarm check (``node_floor_fill_alarm_effect``) when the overlay holds a
``floor_fill_alarm`` section: the check reads the rows the tick just stored, so it runs as often as the fill and sees every
run. A check that cannot run prints its refusal and fails the entry; it never changes what the tick did.

The entry holds no I/O of its own: the handler it builds takes every seam in its constructor. The
default profile is ``inmemory`` (scripted seams over an in-memory conditional store). ``--profile live`` binds the
store, the floor and plan reads, the ledger file, the process runner and the clock from the overlay's
``floor_fill_live`` section; the pool, tickets, brake and stop-signal seams have no live source yet and fail closed
naming what is unbound (see ``container/container_live.py``). Any other profile is refused naming it. The entry ends the process itself at ``floor_fill.command_timeout_seconds``, so a stalled
command leaves no process behind.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Callable, Iterable, Mapping, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

from omnimarket.nodes.node_floor_fill_orchestrator.handlers.handler_floor_fill_command import (
    HandlerFloorFillCommand,
)
from omnimarket.nodes.node_floor_fill_orchestrator.models import (
    EXIT_CODE_BY_STATUS,
    EnumFloorFillCommand,
    EnumFloorFillStatus,
    FloorFillConfigError,
    ModelFloorFillCommandRequest,
    ModelFloorFillConfig,
    ModelFloorFillEntryResult,
    load_floor_fill_config,
)
from omnimarket.nodes.node_floor_fill_orchestrator.models.model_floor_fill_live_settings import (
    LIVE_OVERLAY_SECTION,
    load_floor_fill_live_settings,
)

OVERLAY_SECTION = "floor_fill"
ALARM_OVERLAY_SECTION = "floor_fill_alarm"


def run_entry(
    argv: Sequence[str],
    overlay: Mapping[str, Any],
    *,
    host_id: str,
    hosts: Iterable[tuple[str, bool]],
    build_handler: Callable[[ModelFloorFillConfig], HandlerFloorFillCommand],
) -> ModelFloorFillEntryResult:
    """Parse the command, build the handler from the overlay and run it. A refused overlay prints and fails.

    ``hosts`` is the lab host table as ``(name, available)`` pairs. The configured ``host_id`` must be an available row of
    it before any handler is built, so an unlisted or unavailable host never reaches the local execution path.
    """
    parser = argparse.ArgumentParser(prog="node_floor_fill_orchestrator")
    parser.add_argument("command", choices=[c.value for c in EnumFloorFillCommand])
    parser.add_argument("key", nargs="?", default=None)
    args = parser.parse_args(list(argv))
    try:
        config = load_floor_fill_config(overlay.get(OVERLAY_SECTION, {}))
        config.check_fill_host(hosts)
    except FloorFillConfigError as error:
        return ModelFloorFillEntryResult(
            exit_code=EXIT_CODE_BY_STATUS[EnumFloorFillStatus.FAILED],
            stdout=f"UNKNOWN fill-state:overlay:{','.join(error.keys) or error}\n",
        )
    return build_handler(config).handle(
        ModelFloorFillCommandRequest(
            command=EnumFloorFillCommand(args.command), key=args.key, host_id=host_id
        )
    )


def run_alarm_after_tick(
    result: ModelFloorFillEntryResult,
    command: str,
    overlay: Mapping[str, Any],
    *,
    load_payload: Callable[[str], str | None],
    now: datetime | None = None,
    pool_clock: Callable[[], datetime] | None = None,
) -> ModelFloorFillEntryResult:
    """Run the alarm check over what a tick just stored, when the overlay carries the ``floor_fill_alarm`` section.

    Any command but ``tick``, or an overlay without the section, returns ``result`` unchanged. The check's own line is
    added after the tick's lines; a section the check refuses, or a ledger it cannot read or write, adds the refusal and
    fails the entry (the tick's own failure code stands when it failed).
    """
    section = overlay.get(ALARM_OVERLAY_SECTION)
    if command != EnumFloorFillCommand.TICK.value or section is None:
        return result
    from omnimarket.nodes.node_floor_fill_alarm_effect.container.container_live import (
        run_alarm_check,
    )
    from omnimarket.nodes.node_floor_fill_alarm_effect.models import (
        FloorFillAlarmConfigError,
        load_floor_fill_alarm_settings,
    )

    failed = EXIT_CODE_BY_STATUS[EnumFloorFillStatus.FAILED]
    try:
        settings = load_floor_fill_alarm_settings(section)
    except FloorFillAlarmConfigError as error:
        return _with_alarm_line(
            result,
            f"UNKNOWN alarm-check:overlay:{','.join(error.keys) or error}",
            failed,
        )
    kwargs: dict[str, Any] = {"load_payload": load_payload, "now": now}
    if pool_clock is not None:
        kwargs["pool_clock"] = pool_clock
    try:
        outcome = run_alarm_check(settings, **kwargs)
    except Exception as error:
        return _with_alarm_line(
            result, f"UNKNOWN alarm-check:{type(error).__name__}:{error}", failed
        )
    return _with_alarm_line(
        result,
        f"NOTE alarm-check | alarms={outcome.alarms_found} rows_written={len(outcome.rows_written)} "
        f"sources_unread={','.join(outcome.sources_unread) or 'none'}",
        result.exit_code,
    )


def _with_alarm_line(
    result: ModelFloorFillEntryResult, line: str, exit_code: int
) -> ModelFloorFillEntryResult:
    stdout = (
        result.stdout
        if result.stdout.endswith("\n") or not result.stdout
        else result.stdout + "\n"
    )
    return result.model_copy(
        update={
            "stdout": stdout + line + "\n",
            "exit_code": result.exit_code or exit_code,
        }
    )


def read_env_file_value(path: str, name: str) -> str:
    """The value of ``name`` in a KEY=VALUE env file, or an empty string when the file or key is absent."""
    try:
        lines = Path(path).read_text(encoding="utf-8").splitlines()
    except OSError:
        return ""
    for line in lines:
        key, separator, value = line.partition("=")
        if separator and key.strip() == name:
            return value.strip().strip("\"'")
    return ""


def lab_host_rows(overlay: Mapping[str, Any]) -> list[tuple[str, bool]]:
    """The ``lab_hosts`` rows the caller passes beside the overlay, as ``(name, available)``; none means no host is listed."""
    return [
        (str(row["name"]), bool(row["available"]))
        for row in overlay.get("lab_hosts", [])
        if "name" in row and "available" in row
    ]


def main(argv: Sequence[str] | None = None) -> int:
    arguments = sys.argv[1:] if argv is None else list(argv)
    if arguments and arguments[0] == "schedule":
        from omnimarket.nodes.node_floor_fill_orchestrator.runtime_floor_fill_schedule import (
            main as schedule_main,
        )

        return schedule_main(arguments[1:])
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--overlay-json", required=True)
    parser.add_argument("--this-host", required=True)
    parser.add_argument("--profile", default="inmemory")
    known, rest = parser.parse_known_args(arguments)
    if known.profile not in ("inmemory", "live"):
        sys.stdout.write(
            f"UNKNOWN fill-state:profile-unbound:{known.profile} (no binding of that name)\n"
        )
        return EXIT_CODE_BY_STATUS[EnumFloorFillStatus.FAILED]
    try:
        overlay = json.loads(known.overlay_json)
    except json.JSONDecodeError:
        overlay = None
    if not isinstance(overlay, dict):
        sys.stdout.write("UNKNOWN fill-state:overlay:unreadable\n")
        return EXIT_CODE_BY_STATUS[EnumFloorFillStatus.FAILED]
    build_handler: Callable[[ModelFloorFillConfig], HandlerFloorFillCommand]
    load_payload: Callable[[str], str | None] | None = None
    if known.profile == "live":
        try:
            live_settings = load_floor_fill_live_settings(
                overlay.get(LIVE_OVERLAY_SECTION, {})
            )
        except FloorFillConfigError as error:
            sys.stdout.write(
                f"UNKNOWN fill-state:overlay:{','.join(error.keys) or error}\n"
            )
            return EXIT_CODE_BY_STATUS[EnumFloorFillStatus.FAILED]
        dsn = os.environ.get(live_settings.store_dsn_env, "")
        if not dsn and live_settings.store_dsn_env_file:
            dsn = read_env_file_value(
                live_settings.store_dsn_env_file, live_settings.store_dsn_env
            )
        if not dsn:
            sys.stdout.write(
                f"UNKNOWN fill-state:store-dsn-unset:{live_settings.store_dsn_env}\n"
            )
            return EXIT_CODE_BY_STATUS[EnumFloorFillStatus.FAILED]
        from omnimarket.nodes.node_floor_fill_alarm_effect.container.container_live import (
            store_payload_loader,
        )
        from omnimarket.nodes.node_floor_fill_orchestrator.container.container_live import (
            build_live_handler_factory,
            build_live_store,
        )

        store = build_live_store(live_settings, dsn=dsn)
        build_handler = build_live_handler_factory(live_settings, dsn=dsn, store=store)
        load_payload = store_payload_loader(store)
    else:
        from omnimarket.nodes.node_floor_fill_orchestrator.container.container_inmemory import (
            build_inmemory_handler,
        )

        build_handler = build_inmemory_handler

    result = run_entry(
        rest,
        overlay,
        host_id=known.this_host,
        hosts=lab_host_rows(overlay),
        build_handler=build_handler,
    )
    if load_payload is not None and not result.timed_out:
        result = run_alarm_after_tick(
            result, rest[0] if rest else "", overlay, load_payload=load_payload
        )
    sys.stdout.write(result.stdout)
    sys.stdout.flush()
    if result.timed_out:
        # The command is still running in a worker thread: end the process, so none remains.
        os._exit(result.exit_code)
    return result.exit_code


if __name__ == "__main__":
    sys.exit(main())
