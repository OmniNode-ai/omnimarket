# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The tick appends its rows through the bus append command its unit's environment names, never the ledger file."""

from __future__ import annotations

import json
import shlex
import sys
from pathlib import Path

import pytest

from omnimarket.nodes.node_temporal_health_alarm_effect.runtime_temporal_health_alarm import (
    ledger_appender,
)

pytestmark = pytest.mark.unit

ROW = "2026-11-18T10:00:00Z | STATUS | lane=temporal-health-alarm | src=alarm | state=alarm | Temporal is down"

FAKE_COMMAND = """\
import json, pathlib, sys
args = sys.argv[1:]
pathlib.Path(args[args.index("--out") + 1]).write_text(
    json.dumps({"argv": args, "stdin": sys.stdin.read()}), encoding="utf-8"
)
status = args[args.index("--status") + 1]
print(json.dumps({"status": status, "message": "from the fake command"}))
sys.exit(65 if status == "refused" else 0)
"""


def _environment(tmp_path: Path, status: str) -> tuple[dict[str, str], Path]:
    script = tmp_path / "fake_append.py"
    script.write_text(FAKE_COMMAND, encoding="utf-8")
    out = tmp_path / "seen.json"
    command = shlex.join(
        [sys.executable, str(script), "--out", str(out), "--status", status]
    )
    return (
        {
            "ONEX_LEDGER_BUS_APPEND_COMMAND": command,
            "ONEX_LEDGER_HOST_NAME": "h-test",
        },
        out,
    )


def test_row_goes_to_the_bus_command_with_lane_host_and_request_cell(
    tmp_path: Path,
) -> None:
    environ, out = _environment(tmp_path, "accepted")
    assert ledger_appender(environ)(ROW) == ""
    seen = json.loads(out.read_text(encoding="utf-8"))
    assert seen["argv"][seen["argv"].index("--lane") + 1] == "temporal-health-alarm"
    assert seen["argv"][seen["argv"].index("--host") + 1] == "h-test"
    assert seen["stdin"].startswith(ROW + " | req=")


def test_a_refused_append_is_an_error_text_not_a_raise(tmp_path: Path) -> None:
    environ, _ = _environment(tmp_path, "refused")
    error = ledger_appender(environ)(ROW)
    assert error.startswith("ledger bus append refused")
    assert "from the fake command" in error


@pytest.mark.parametrize(
    "missing", ["ONEX_LEDGER_BUS_APPEND_COMMAND", "ONEX_LEDGER_HOST_NAME"]
)
def test_unset_route_is_named_and_nothing_is_run(tmp_path: Path, missing: str) -> None:
    environ, out = _environment(tmp_path, "accepted")
    del environ[missing]
    error = ledger_appender(environ)(ROW)
    assert missing in error
    assert not out.exists()
