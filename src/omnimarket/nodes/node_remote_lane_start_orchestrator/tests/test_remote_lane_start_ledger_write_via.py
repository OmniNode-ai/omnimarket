# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-22848: the lane start leader's CLAIM is written on a host whose ledger of record is the bus.

On such a host the ledger writer refuses a direct file append from anyone but the
ledger serve. The serve's runner forces ``ONEX_LEDGER_WRITE_VIA=local``; the lane
start leader is not the serve, so its overlay names ``ledger_write_via`` and a
bus-of-record host gets ``bus``. The writer here is a scripted stand-in with the
same refusal rule as the ledger writer's record switch.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from omnimarket.nodes.node_remote_lane_start_orchestrator.models.model_remote_lane_start import (
    ModelRemoteLaneStartSettings,
)
from omnimarket.nodes.node_remote_lane_start_orchestrator.tests import (
    lane_start_fixtures as fx,
)
from omnimarket.nodes.node_work_ledger_append_effect.protocols.local_ledger_append_command import (
    LocalLedgerAppendCommand,
)

WRITER = """\
import os, sys
ledger = sys.argv[1]
row = sys.argv[sys.argv.index("--append") + 1]
if os.environ.get("ONEX_LEDGER_RECORD") == "bus" and os.environ.get("ONEX_LEDGER_WRITE_VIA") == "local":
    print("DIRECT FILE APPEND REFUSED -- ONEX_LEDGER_RECORD=bus", file=sys.stderr)
    sys.exit(3)
with open(ledger, "a", encoding="utf-8") as out:
    out.write(row + "\\n")
"""

ROW = "2026-11-07T00:00:00Z | CLAIM | lane=l | ticket=OMN-22848 | text"


def _runner(tmp_path: Path, write_via: str) -> tuple[LocalLedgerAppendCommand, Path]:
    writer = tmp_path / "writer.py"
    writer.write_text(WRITER, encoding="utf-8")
    ledger = tmp_path / "ledger.md"
    ledger.write_text("", encoding="utf-8")
    return (
        LocalLedgerAppendCommand(
            [sys.executable, str(writer), str(ledger)], write_via=write_via
        ),
        ledger,
    )


def test_bus_record_host_refuses_the_serve_mode_append(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Control: the serve's local mode is refused on a bus-of-record host and writes nothing."""
    monkeypatch.setenv("ONEX_LEDGER_RECORD", "bus")
    runner, ledger = _runner(tmp_path, "local")
    result = runner.append(ROW)
    assert result.exit_code == 3
    assert "DIRECT FILE APPEND REFUSED" in result.stderr
    assert ledger.read_text(encoding="utf-8") == ""


def test_lane_start_bus_mode_append_is_written_on_a_bus_record_host(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ONEX_LEDGER_RECORD", "bus")
    runner, ledger = _runner(tmp_path, fx.settings().ledger_write_via)
    result = runner.append(ROW)
    assert result.exit_code == 0, result.stderr
    assert ledger.read_text(encoding="utf-8") == ROW + "\n"


def test_ledger_write_via_is_a_required_overlay_key() -> None:
    section = fx.settings().model_dump(mode="json")
    section.pop("ledger_write_via")
    with pytest.raises(KeyError, match=r"remote_lane_start\.ledger_write_via"):
        ModelRemoteLaneStartSettings.from_overlay({"remote_lane_start": section})


def test_an_unknown_write_mode_is_refused() -> None:
    with pytest.raises(ValueError, match="write_via must be one of"):
        LocalLedgerAppendCommand(["writer"], write_via="file")
