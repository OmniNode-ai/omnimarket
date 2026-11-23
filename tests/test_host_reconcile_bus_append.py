# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The reconcile failure row goes out through the bus append command, never a ledger file path.

The writer is the bus append CLI's shape (`--lane --host --request-id --rows-file -`, the row on
stdin). Each test fails on the code that ran `onex-ledger <ledger path> --append <row>`.
"""

import inspect
import socket
from pathlib import Path

import pytest

from omnimarket.nodes.node_host_reconcile_effect.handlers.reconcile_failure_alarm import (
    LEDGER_LANE,
    append_friction,
)
from tests.test_host_reconcile_effect import (
    RecordingEvaluator,
    RecordingPublisher,
    command,
    handler,
    setup_workspace,
)

pytestmark = pytest.mark.unit

ROW = "2026-11-21T20:00:00Z | FRICTION | lane=x | planted"


def _bus_writer(root: Path) -> tuple[str, Path, Path]:
    """A stand-in bus append command: records its argv and the stdin row, never opens a ledger."""
    argv_out, rows_out = root / "argv.txt", root / "rows.txt"
    script = root / "bus_writer.sh"
    script.write_text(
        f'printf "%s\\n" "$*" > "{argv_out}"\ncat >> "{rows_out}"\n', encoding="utf-8"
    )
    return f"bash {script}", argv_out, rows_out


def test_append_friction_takes_no_ledger_path() -> None:
    assert "ledger" not in inspect.signature(append_friction).parameters


def test_the_row_goes_to_the_append_command_on_stdin(tmp_path: Path) -> None:
    writer, argv_out, rows_out = _bus_writer(tmp_path)
    failed = append_friction(append_command=writer, row=ROW, host="h051")
    assert failed is None
    assert rows_out.read_text(encoding="utf-8").strip() == ROW
    words = argv_out.read_text(encoding="utf-8").split()
    assert words[words.index("--lane") + 1] == LEDGER_LANE
    assert words[words.index("--host") + 1] == "h051"
    assert words[words.index("--rows-file") + 1] == "-"
    assert "--request-id" in words


def test_no_append_command_writes_nothing(tmp_path: Path) -> None:
    assert append_friction(append_command="", row=ROW, host="h051") is None


def test_a_failing_writer_is_a_reason_not_an_exception() -> None:
    failed = append_friction(append_command="false", row=ROW, host="h051")
    assert failed is not None
    assert "ledger writer exited 1" in failed


def test_a_failed_reconcile_writes_its_row_with_no_ledger_path_named(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = setup_workspace(tmp_path, monkeypatch)
    writer, _, rows_out = _bus_writer(root)
    cmd = command(
        root,
        target_host=socket.gethostname() + "-other",
        ledger="",
        ledger_append_command=writer,
    )
    result = handler(RecordingEvaluator(), RecordingPublisher()).handle(cmd)
    assert result.exit_code == 3
    assert "FRICTION" in rows_out.read_text(encoding="utf-8")
