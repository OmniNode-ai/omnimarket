# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""``python -m omnimarket.nodes.node_delegation_scenario_alarm_effect.runtime_delegation_scenario_alarm_tick``.

The scheduled tick of the delegation scenario alarm: read each watched scenario's last-run record under the home directory
and append one alarm row per failed run through the bus append command the unit names. ``--home`` replaces the home
directory (a sandbox run over a forced failing record). Prints one JSON line per scenario. Exits 1 when an alarm row could
not be appended, so the unit itself reads failed and the liveness reading sees it; a failed scenario alone exits 0.
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
        return 2
    home = args.home or Path(env.get(settings.home_env, "") or Path.home())
    host = env.get(settings.host_env, "").strip() or socket.gethostname()
    handler = HandlerDelegationScenarioAlarm(
        settings,
        CommandLedgerAppender(
            append_argv, settings.lane, host, settings.ledger_append_timeout_seconds
        ),
    )
    result = handler.handle(ModelScenarioAlarmRequest(home=home, host=host))
    for reading in result.readings:
        sys.stdout.write(
            json.dumps(reading.model_dump(mode="json"), sort_keys=True) + "\n"
        )
    if args.state_dir is not None:
        write_atomic(
            args.state_dir / "last-run.json",
            json.dumps(
                {
                    "alarms": [r.scenario for r in result.alarms],
                    "conclusion": "failure" if result.unappended else "success",
                    "ran_at": datetime.now(UTC).isoformat(),
                    "unappended": [r.scenario for r in result.unappended],
                },
                sort_keys=True,
            ),
        )
    return 1 if result.unappended else 0


if __name__ == "__main__":
    sys.exit(main())
