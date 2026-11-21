# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One tick of the lane dispatcher, the unit the host installer renders.

The unit passes ``--omni-home`` and ``--state-dir`` and declares ``ONEX_LEDGER_BUS_READ_COMMAND`` (how MSG rows are read from the
ledger of record over the bus), ``ONEX_LANE_DISPATCH_BUS_APPEND`` (the bus publisher command rows are written through),
``ONEX_LEDGER_HOST_NAME``, ``ONEX_LANE_DISPATCH_PARENT`` and ``ONEX_LANE_DISPATCH_PLUGIN_ROOT`` (the omni plugin's root, where the runner and the lane-brief skill live). Exit 0: every request this tick took was placed.
Exit 1: a placement failed or no host admitted a lane. Exit 3: the unit supplies no read or append command.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

from omnimarket.nodes.node_lane_dispatch_intake_effect.handlers.adapter_lane_dispatch import (
    BusRowWriter,
    RemoteLanePlacer,
    ScriptBriefBuilder,
)
from omnimarket.nodes.node_lane_dispatch_intake_effect.handlers.handler_lane_dispatch_intake import (
    HandlerLaneDispatchIntake,
    rejected_key,
)
from omnimarket.nodes.node_lane_dispatch_intake_effect.models.model_lane_dispatch_intake import (
    EnumPlacementStatus,
    ModelLaneDispatchIntakeRequest,
)
from omnimarket.nodes.node_work_ledger_host_mirror_effect.handlers.adapter_bus_read import (
    BusLedgerRowsReader,
)
from omnimarket.nodes.node_work_ledger_host_mirror_effect.protocols import (
    LedgerReadError,
)

NODE_NAME = "node_lane_dispatch_intake_effect"
PLUGIN_ROOT_ENV = "ONEX_LANE_DISPATCH_PLUGIN_ROOT"
RUNNER_RELATIVE = "skills/remote-lane/scripts/onex_remote_lane.py"
READ_COMMAND_ENV = "ONEX_LEDGER_BUS_READ_COMMAND"
APPEND_COMMAND_ENV = "ONEX_LANE_DISPATCH_BUS_APPEND"
READ_TIMEOUT_SECONDS = 25.0
WINDOW_SECONDS = 86400
MAX_PAGES = 10
PAGE_SIZE = 200


def _write_atomically(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    held = path.with_suffix(path.suffix + ".tmp")
    held.write_text(text, encoding="utf-8")
    held.replace(path)


def _load_state(path: Path) -> tuple[set[str], set[str]]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return set(data["done"]), set(data["noted"])
    except (OSError, ValueError, KeyError, TypeError):
        return set(), set()


def _dispatch_rows(command: list[str]) -> tuple[str, ...]:
    """The MSG rows of the last day, read over the bus; the handler keeps those addressed to the dispatcher."""
    reader = BusLedgerRowsReader(command, READ_TIMEOUT_SECONDS, ("MSG",))
    since = datetime.now(UTC) - timedelta(seconds=WINDOW_SECONDS)
    rows: list[str] = []
    cursor: str | None = None
    for _ in range(MAX_PAGES):
        page = reader.read_rows(since=since, cursor=cursor, page_size=PAGE_SIZE)
        rows.extend(row.raw_row for row in page.rows if "labdispatch" in row.raw_row)
        cursor = page.next_cursor
        if cursor is None:
            break
    return tuple(rows)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog=f"{NODE_NAME}.tick", description=__doc__)
    parser.add_argument("--omni-home", dest="registry_root", required=True, type=Path)
    parser.add_argument("--state-dir", required=True, type=Path)
    parser.add_argument("--max-placements", type=int, default=1)
    args = parser.parse_args(argv)
    ran_at = datetime.now(UTC).isoformat()
    read_command = shlex.split(os.environ.get(READ_COMMAND_ENV, ""))
    append_command = shlex.split(os.environ.get(APPEND_COMMAND_ENV, ""))
    if not read_command or not append_command or not os.environ.get(PLUGIN_ROOT_ENV):
        message = f"{READ_COMMAND_ENV}, {APPEND_COMMAND_ENV} and {PLUGIN_ROOT_ENV} are not all set: the unit declares them"
        sys.stderr.write(f"INDETERMINATE: {message}\n")
        _write_atomically(
            args.state_dir / "last-run.json",
            json.dumps({"ran_at": ran_at, "exit_code": 3, "indeterminate": message}),
        )
        return 3
    plugin_root = Path(os.environ[PLUGIN_ROOT_ENV])
    handler = HandlerLaneDispatchIntake(
        ScriptBriefBuilder(plugin_root, args.state_dir / "briefs"),
        RemoteLanePlacer(
            plugin_root / RUNNER_RELATIVE,
            os.environ.get("ONEX_LANE_DISPATCH_PARENT", "labdispatch"),
        ),
        BusRowWriter(
            append_command,
            "labdispatch",
            os.environ.get("ONEX_LEDGER_HOST_NAME", ""),
            args.state_dir / "rows",
        ),
    )
    state_path = args.state_dir / "state.json"
    done, noted = _load_state(state_path)
    try:
        result = handler.handle(
            ModelLaneDispatchIntakeRequest(
                rows=_dispatch_rows(read_command),
                done=frozenset(done),
                noted=frozenset(noted),
                max_placements=args.max_placements,
            )
        )
    except LedgerReadError as exc:
        sys.stderr.write(f"REFUSED: {exc}\n")
        _write_atomically(
            args.state_dir / "last-run.json",
            json.dumps({"ran_at": ran_at, "exit_code": 1, "refused": str(exc)}),
        )
        return 1
    for outcome in result.outcomes:
        (noted if outcome.status is EnumPlacementStatus.NO_HOST else done).add(
            outcome.key
        )
    done.update(rejected_key(r) for r in result.rejected)
    _write_atomically(
        state_path, json.dumps({"done": sorted(done), "noted": sorted(noted)})
    )
    code = (
        0 if all(o.status is EnumPlacementStatus.PLACED for o in result.outcomes) else 1
    )
    for outcome in result.outcomes:
        sys.stdout.write(
            f"{NODE_NAME} {outcome.lane} {outcome.status.value} host={outcome.host} exit={outcome.exit_code}\n"
        )
    sys.stdout.write(
        f"{NODE_NAME} pending_after={result.pending_after} rejected={len(result.rejected)}\n"
    )
    _write_atomically(
        args.state_dir / "last-run.json",
        json.dumps(
            {
                "ran_at": ran_at,
                "exit_code": code,
                "result": result.model_dump(mode="json"),
            }
        ),
    )
    return code


if __name__ == "__main__":
    sys.exit(main())
