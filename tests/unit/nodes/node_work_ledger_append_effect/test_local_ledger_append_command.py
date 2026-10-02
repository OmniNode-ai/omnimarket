# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Local subprocess boundary and timeout (OMN-20275)."""

import subprocess
from pathlib import Path
from typing import Any

import pytest

from omnimarket.nodes.node_work_ledger_append_effect.protocols import (
    LocalLedgerAppendCommand,
    LocalLedgerFile,
)
from omnimarket.nodes.node_work_ledger_append_effect.protocols import (
    local_ledger_append_command as local,
)

pytestmark = pytest.mark.unit


def test_command_keeps_rows_one_argument_without_shell(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[tuple[list[str], dict[str, Any]]] = []

    def run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        seen.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, 65, "stdout", "refusal")

    monkeypatch.setattr(local.subprocess, "run", run)
    rows = "row with 'quotes' $(not executed)\ncontinuation"
    result = LocalLedgerAppendCommand(["ledger-lock", "ledger file"], 2.5).append(rows)
    assert result.exit_code == 65
    assert result.stdout == "stdout"
    assert result.stderr == "refusal"
    assert len(seen) == 1
    argv, kwargs = seen[0]
    assert argv == ["ledger-lock", "ledger file", "--append", rows, "--timeout", "60"]
    env = kwargs.pop("env")
    # The child never re-publishes to the bus a request came from.
    assert env["ONEX_LEDGER_WRITE_VIA"] == "local"
    assert kwargs == {
        "stdin": subprocess.DEVNULL,
        "capture_output": True,
        "text": True,
        "timeout": 2.5,
        "check": False,
    }


def test_timeout_returns_124_and_preserves_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(
            argv, kwargs["timeout"], output=b"partial out", stderr=b"partial err"
        )

    monkeypatch.setattr(local.subprocess, "run", run)
    result = LocalLedgerAppendCommand(["ledger-lock"], 0.01).append("row")
    assert result.exit_code == 124
    assert result.stdout == "partial out"
    assert result.stderr == "partial err"


def test_unlaunchable_command_returns_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        raise FileNotFoundError("no append executable")

    monkeypatch.setattr(local.subprocess, "run", run)
    result = LocalLedgerAppendCommand(["missing"]).append("row")
    assert result.exit_code == 70
    assert "no append executable" in result.stderr


def test_local_reader_reads_exact_file(tmp_path: Path) -> None:
    ledger = tmp_path / "ledger.md"
    ledger.write_text("row\ncontinuation\n", encoding="utf-8")
    assert LocalLedgerFile(ledger).read_text() == "row\ncontinuation\n"


def test_empty_argv_refused() -> None:
    with pytest.raises(ValueError, match="empty"):
        LocalLedgerAppendCommand([])
