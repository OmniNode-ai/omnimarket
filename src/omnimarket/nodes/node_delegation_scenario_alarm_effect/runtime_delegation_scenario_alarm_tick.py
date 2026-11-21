# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""``python -m omnimarket.nodes.node_delegation_scenario_alarm_effect.runtime_delegation_scenario_alarm_tick``.

The scheduled tick of the delegation scenario alarm: read each watched scenario's last-run record under the home directory
and append one alarm row per failed run through the bus append command the unit names. ``--home`` replaces the home
directory (a sandbox run over a forced failing record). Prints one JSON line per scenario. Exits 1 when an alarm row could
not be appended or the handler raised, and 2 when the tick has no ledger route, so the unit itself reads failed and the
liveness reading sees it; a failed scenario alone exits 0. Every exit writes the tick's own last-run record under
``--state-dir``: the liveness reading of this tick (R9) reads that record, so a tick that ended in exit 1 or 2 must leave
one that says ``failure``, never the previous tick's ``success``.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import socket
import sys
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path

from omnimarket.handlers.delegation_scenario_runtime import write_atomic
from omnimarket.nodes.node_delegation_scenario_alarm_effect.handlers.handler_delegation_scenario_alarm import (
    HandlerDelegationScenarioAlarm,
    ModelScenarioAlarmRequest,
    load_alarm_settings,
)
from omnimarket.nodes.node_reading_writer_effect.adapters.ledger_adapters import (
    CommandLedgerAppender,
)


def _write_last_run(
    state_dir: Path | None,
    *,
    alarms: Sequence[str],
    unappended: Sequence[str],
    error: str | None = None,
) -> None:
    """Write this tick's last-run record; ``error`` (a tick that could not read or append) makes it a failure."""
    if state_dir is None:
        return
    record: dict[str, object] = {
        "alarms": list(alarms),
        "conclusion": "failure" if unappended or error else "success",
        "ran_at": datetime.now(UTC).isoformat(),
        "unappended": list(unappended),
    }
    if error is not None:
        record["error"] = error
    write_atomic(state_dir / "last-run.json", json.dumps(record, sort_keys=True))


def main(
    argv: Sequence[str] | None = None,
    *,
    environ: Mapping[str, str] | None = None,
) -> int:
    parser = argparse.ArgumentParser(prog="node_delegation_scenario_alarm_effect")
    parser.add_argument(
        "--home", type=Path, default=None, help="read the records under this directory"
    )
    parser.add_argument("--omni-home", type=Path, default=None)
    parser.add_argument(
        "--state-dir",
        type=Path,
        default=None,
        help="write this tick's last-run record here",
    )
    args = parser.parse_args(argv)
    env = dict(os.environ if environ is None else environ)  # ONEX_FLAG_EXEMPT
    settings = load_alarm_settings()
    append_argv = shlex.split(env.get(settings.ledger_append_command_env, ""))
    if not append_argv:
        sys.stderr.write(
            f"{settings.ledger_append_command_env} is not set; an alarm has no ledger route\n"
        )
        _write_last_run(
            args.state_dir,
            alarms=(),
            unappended=(),
            error=f"{settings.ledger_append_command_env} is not set",
        )
        return 2
    home = args.home or Path(env.get(settings.home_env, "") or Path.home())
    host = env.get(settings.host_env, "").strip() or socket.gethostname()
    handler = HandlerDelegationScenarioAlarm(
        settings,
        CommandLedgerAppender(
            append_argv, settings.lane, host, settings.ledger_append_timeout_seconds
        ),
    )
    try:
        result = handler.handle(ModelScenarioAlarmRequest(home=home, host=host))
    except (
        Exception
    ) as exc:  # the tick's own record must say failure whatever the appender raised
        _write_last_run(
            args.state_dir,
            alarms=(),
            unappended=(),
            error=f"{type(exc).__name__}: {exc}"[:300],
        )
        sys.stderr.write(f"the alarm tick failed: {type(exc).__name__}: {exc}\n")
        return 1
    for reading in result.readings:
        sys.stdout.write(
            json.dumps(reading.model_dump(mode="json"), sort_keys=True) + "\n"
        )
    _write_last_run(
        args.state_dir,
        alarms=[r.scenario for r in result.alarms],
        unappended=[r.scenario for r in result.unappended],
    )
    return 1 if result.unappended else 0


if __name__ == "__main__":
    sys.exit(main())
