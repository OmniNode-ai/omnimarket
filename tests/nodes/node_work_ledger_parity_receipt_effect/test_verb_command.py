# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The receipt writer passes the ledger-reader command to the parity check (OMN-24222)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

import omnimarket.nodes.node_projection_work_ledger.parity as parity_module
from omnimarket.nodes.node_friction_rollup_tick_effect.protocols.protocol_ledger_appender import (
    InMemoryLedgerAppender,
)
from omnimarket.nodes.node_work_ledger_parity_receipt_effect.handlers.handler_parity_receipt import (
    HandlerParityReceipt,
)
from omnimarket.nodes.node_work_ledger_parity_receipt_effect.handlers.parity_receipt_core import (
    receipt_rows,
    row_pattern,
)
from omnimarket.nodes.node_work_ledger_parity_receipt_effect.runtime_parity_receipt import (
    main,
)
from tests.nodes.node_work_ledger_parity_receipt_effect.support import (
    CLEAN,
    NOW,
    request_for,
    write_world,
)

pytestmark = pytest.mark.unit


def _argv(tmp_path: Path) -> list[str]:
    return (tmp_path / "parity-argv.txt").read_text(encoding="utf-8").splitlines()


def test_passes_the_verb_command(tmp_path: Path) -> None:
    ledger, fields = write_world(tmp_path, [])
    handler = HandlerParityReceipt(appender=InMemoryLedgerAppender())
    result = handler.handle(
        request_for(tmp_path, ledger, fields, verb_command="onex-ledger")
    )
    assert result.state == "APPENDED"
    argv = _argv(tmp_path)
    assert "--verb-command" in argv
    assert argv[argv.index("--verb-command") + 1] == "onex-ledger"


def test_unchanged_without_a_verb_command(tmp_path: Path) -> None:
    ledger, fields = write_world(tmp_path, [])
    handler = HandlerParityReceipt(appender=InMemoryLedgerAppender())
    result = handler.handle(request_for(tmp_path, ledger, fields))
    assert result.state == "APPENDED"
    assert "--verb-command" not in _argv(tmp_path)


def test_blank_verb_command_is_not_set(tmp_path: Path) -> None:
    ledger, fields = write_world(tmp_path, [])
    handler = HandlerParityReceipt(appender=InMemoryLedgerAppender())
    result = handler.handle(request_for(tmp_path, ledger, fields, verb_command="   "))
    assert result.state == "APPENDED"
    assert "--verb-command" not in _argv(tmp_path)


def test_seam_with_the_parity_check(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        parity_module.main(["--help"])
    assert "--verb-command" in capsys.readouterr().out
    row = CLEAN.replace(
        "exact=yes | ",
        "exact=yes | exact_kinds=ACK:yes | verb_diff=query:0,holds:0 | ",
    )
    assert row_pattern("work-ledger-parity").match(row) is not None
    rows = receipt_rows(row + "\n", "work-ledger-parity")
    assert len(rows) == 1
    assert rows[0].clean


def test_unit_entry_reads_the_verb_command_from_its_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The unit's own entry carries ONEX_LEDGER_PARITY_VERB_COMMAND through to the parity check's argv."""
    ledger, fields = write_world(tmp_path, [])
    stub = tmp_path / "stub_ledger.py"
    stub.write_text("import sys\n", encoding="utf-8")
    monkeypatch.setenv("ONEX_LEDGER_COMMAND", f"{sys.executable} {stub}")
    monkeypatch.setenv("ONEX_LEDGER_PARITY_PYTHON", fields["parity_python"])
    monkeypatch.setenv("ONEX_LEDGER_PARITY_OVERLAY", fields["dsn_overlay_path"])
    monkeypatch.setenv("ONEX_LEDGER_PARITY_ENV_FILE", fields["dsn_env_file"])
    monkeypatch.setenv("ONEX_LEDGER_PARITY_PG_PORT", fields["pg_port"])
    monkeypatch.setenv("ONEX_LEDGER_PARITY_VERB_COMMAND", "onex-ledger")
    code = main(
        [
            "--omni-home",
            str(tmp_path / "registry"),
            "--ledger",
            str(ledger),
            "--now",
            NOW,
        ]
    )
    assert code == 0
    argv = _argv(tmp_path)
    assert argv[argv.index("--verb-command") + 1] == "onex-ledger"
