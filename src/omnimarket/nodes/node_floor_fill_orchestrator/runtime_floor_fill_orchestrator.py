# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Entry: ``python -m <this module> --overlay-json <J> --this-host <H> <command> [key]``.

This is the node's runtime-module form (the census node's form): the tick unit and the prompt-submit hook entry
both run it with the dispatch venv's interpreter. ``J`` is the overlay's ``floor_fill`` section wrapped as
``{"floor_fill": {...}}``; an empty or unreadable ``J`` prints ``UNKNOWN fill-state:overlay:unreadable`` and fails.

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
from typing import Any

import yaml

from omnimarket.handlers.node_overlay_reader import find_node_overlay
from omnimarket.handlers.overlay_keys import resolve_lab_hosts_table
from omnimarket.nodes.node_floor_fill_orchestrator.handlers.handler_floor_fill_command import (
    HandlerFloorFillCommand,
)
from omnimarket.nodes.node_floor_fill_orchestrator.models import (
    EXIT_CODE_BY_STATUS,
    EnumFloorFillCommand,
    EnumFloorFillStatus,
    FloorFillConfigError,
    ModelFloorFillAnswer,
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
OVERLAY_NODE = "node_floor_fill_orchestrator"
UNKNOWN_PREFIX = "UNKNOWN "


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


def answer_json_line(
    answer: ModelFloorFillAnswer | None, *, stdout: str = "", exit_code: int = 0
) -> str:
    """The one JSON line a scheduled caller reads after the entry's own lines (``--answer-json``).

    It carries the typed answer's counts: ``free_slots`` (None when the pool was not read), ``unowned_work``, the lanes
    this run placed, ``unread`` (each source that was not read) and ``why_none``, the first line of the answer when no lane
    was placed. An entry that has no typed answer (a refused overlay, an unset store DSN, an unreachable fill host) is
    reported from its printed first line and its exit code, so a refusal is read as a refusal and never as zero slots.
    """
    lines = list(answer.lines) if answer is not None else stdout.splitlines()
    first = lines[0] if lines else ""
    if answer is None:
        unread = (
            first.removeprefix(UNKNOWN_PREFIX)
            if first.startswith(UNKNOWN_PREFIX)
            else ""
        )
        record: dict[str, Any] = {
            "status": EnumFloorFillStatus.FAILED.value,
            "exit_code": exit_code,
            "free_slots": None,
            "unowned_work": None,
            "placed_lanes": [],
            "unread": unread,
            "why_none": first or "the fill printed nothing",
        }
        return json.dumps(record, sort_keys=True)
    placed = [r.lane for r in answer.key_results if r.kind == "started" and r.lane]
    unread = ",".join(answer.unread)
    if not unread and first.startswith(UNKNOWN_PREFIX):
        unread = first.removeprefix(UNKNOWN_PREFIX)
    record = {
        "status": answer.status.value,
        "exit_code": answer.exit_code,
        "free_slots": answer.free_slots,
        "unowned_work": answer.unowned_work,
        "placed_lanes": placed,
        "unread": unread,
        "why_none": "" if placed else (first or answer.status.value),
    }
    return json.dumps(record, sort_keys=True)


def _overlay_from_json(text: str | None) -> Mapping[str, Any] | None:
    try:
        loaded = json.loads(text or "")
    except json.JSONDecodeError:
        return None
    return loaded if isinstance(loaded, dict) else None


def overlay_from_roots() -> Mapping[str, Any] | None:
    """The entry's overlay as the deployment holds it: ``<root>/node_floor_fill_orchestrator/overlay.yaml`` under
    ``ONEX_SKILL_OVERLAY_ROOTS`` and the lab host table (the file's own ``lab_hosts`` rows when it carries them).

    ``None`` when no root supplies the overlay or it is not a mapping, so the caller prints the one overlay refusal.
    """
    found = find_node_overlay(OVERLAY_NODE)
    if found is None:
        return None
    loaded = yaml.safe_load(found.read_text(encoding="utf-8"))
    if not isinstance(loaded, dict):
        return None
    if "lab_hosts" not in loaded:
        table = resolve_lab_hosts_table()
        loaded = {
            **loaded,
            "lab_hosts": [
                {"name": row.name, "available": row.available} for row in table.hosts
            ],
        }
    return loaded


def _refused(line: str, *, answer_json: bool) -> int:
    code = EXIT_CODE_BY_STATUS[EnumFloorFillStatus.FAILED]
    sys.stdout.write(line + "\n")
    if answer_json:
        sys.stdout.write(
            answer_json_line(None, stdout=line + "\n", exit_code=code) + "\n"
        )
    return code


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
    parser.add_argument("--overlay-json", default=None)
    parser.add_argument("--overlay-from-roots", action="store_true")
    parser.add_argument("--this-host", default=None)
    parser.add_argument("--profile", default="inmemory")
    parser.add_argument("--answer-json", action="store_true")
    known, rest = parser.parse_known_args(arguments)
    if known.profile not in ("inmemory", "live"):
        return _refused(
            f"UNKNOWN fill-state:profile-unbound:{known.profile} (no binding of that name)",
            answer_json=known.answer_json,
        )
    overlay = (
        overlay_from_roots()
        if known.overlay_from_roots
        else _overlay_from_json(known.overlay_json)
    )
    if not isinstance(overlay, dict):
        return _refused(
            "UNKNOWN fill-state:overlay:unreadable", answer_json=known.answer_json
        )
    this_host = known.this_host or str(
        overlay.get(OVERLAY_SECTION, {}).get("host_id", "")
    )
    if not this_host:
        return _refused(
            "UNKNOWN fill-state:overlay:unreadable", answer_json=known.answer_json
        )
    build_handler: Callable[[ModelFloorFillConfig], HandlerFloorFillCommand]
    if known.profile == "live":
        try:
            live_settings = load_floor_fill_live_settings(
                overlay.get(LIVE_OVERLAY_SECTION, {})
            )
        except FloorFillConfigError as error:
            return _refused(
                f"UNKNOWN fill-state:overlay:{','.join(error.keys) or error}",
                answer_json=known.answer_json,
            )
        dsn = os.environ.get(live_settings.store_dsn_env, "")
        if not dsn:
            return _refused(
                f"UNKNOWN fill-state:store-dsn-unset:{live_settings.store_dsn_env}",
                answer_json=known.answer_json,
            )
        from omnimarket.nodes.node_floor_fill_orchestrator.container.container_live import (
            build_live_handler_factory,
        )

        build_handler = build_live_handler_factory(live_settings, dsn=dsn)
    else:
        from omnimarket.nodes.node_floor_fill_orchestrator.container.container_inmemory import (
            build_inmemory_handler,
        )

        build_handler = build_inmemory_handler

    result = run_entry(
        rest,
        overlay,
        host_id=this_host,
        hosts=lab_host_rows(overlay),
        build_handler=build_handler,
    )
    sys.stdout.write(result.stdout)
    if known.answer_json:
        sys.stdout.write(
            answer_json_line(
                result.answer, stdout=result.stdout, exit_code=result.exit_code
            )
            + "\n"
        )
    sys.stdout.flush()
    if result.timed_out:
        # The command is still running in a worker thread: end the process, so none remains.
        os._exit(result.exit_code)
    return result.exit_code


if __name__ == "__main__":
    sys.exit(main())
