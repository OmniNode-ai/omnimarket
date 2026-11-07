# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The real-backend overlay: a ledger file read, a real append command, a real subprocess check (OMN-23013 AC1).

`python -m omnimarket.nodes.node_commitment_check_runner_effect` is what the schedule's unit runs. Here it runs against a
ledger file and an append command that writes the file, so the commitment closes through the same ports the deployment
uses, with only the paths synthetic.
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import yaml

from omnimarket.nodes.node_commitment_check_runner_effect.runtime_commitment_check import (
    main,
)
from omnimarket.nodes.node_commitment_ledger_compute.handlers.handler_commitment_ledger import (
    HandlerCommitmentLedger,
)
from omnimarket.nodes.node_commitment_ledger_compute.models import (
    ModelCommitmentLedgerRequest,
)
from tests.nodes.node_commitment_check_runner_effect.support import (
    NODES,
    ledger_rules,
)

pytestmark = pytest.mark.unit

APPEND_SCRIPT = """\
import json, sys
path = sys.argv[1]
with open(path, "a", encoding="utf-8") as ledger:
    ledger.write(sys.stdin.read())
print(json.dumps({"status": "accepted", "message": "synthetic"}))
"""


def _backend(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, check: str) -> Path:
    ledger = tmp_path / "ROLLING_WORK_LEDGER.md"
    opened = (
        HandlerCommitmentLedger()
        .handle(
            ModelCommitmentLedgerRequest(
                operation="build_open_row",
                rules=ledger_rules(),
                now=datetime.now(UTC) - timedelta(hours=3),
                lane="orchestrator-31d9",
                ask="Bring the host up and tell me it reads clean",
                owner="host-bringup-1",
                check=check,
            )
        )
        .row
    )
    assert opened is not None
    ledger.write_text(opened + "\n", encoding="utf-8")
    script = tmp_path / "append.py"
    script.write_text(APPEND_SCRIPT, encoding="utf-8")
    overlay = yaml.safe_load(
        (NODES / "node_commitment_check_runner_effect/overlay.example.yaml").read_text()
    )
    overlay["allowed_executables"] = [sys.executable]
    overlay_file = tmp_path / "runner-overlay.yaml"
    overlay_file.write_text(yaml.safe_dump(overlay), encoding="utf-8")
    monkeypatch.setenv("ONEX_COMMITMENT_CHECK_OVERLAY", str(overlay_file))
    monkeypatch.setenv(
        "ONEX_COMMITMENT_OVERLAY",
        str(NODES / "node_commitment_ledger_compute/overlay.example.yaml"),
    )
    monkeypatch.setenv("ONEX_LEDGER_PATH", str(ledger))
    monkeypatch.setenv(
        "ONEX_LEDGER_APPEND_COMMAND", f"{sys.executable} {script} {ledger}"
    )
    monkeypatch.setenv("ONEX_HOST_NAME", "test-host")
    return ledger


def test_the_cli_closes_a_commitment_whose_check_passes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    ledger = _backend(
        tmp_path, monkeypatch, f'{sys.executable} -c "raise SystemExit(0)"'
    )
    assert main(["--correlation-id", "cli-1"]) == 0
    text = ledger.read_text(encoding="utf-8")
    assert "status=verified" in text
    assert "exit=0" in text
    assert "open->verified verified exit=0" in capsys.readouterr().out


def test_failing_control_the_cli_leaves_a_failing_check_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    ledger = _backend(
        tmp_path, monkeypatch, f'{sys.executable} -c "raise SystemExit(4)"'
    )
    assert main([]) == 0
    assert "status=verified" not in ledger.read_text(encoding="utf-8")
    assert "still-open exit=4" in capsys.readouterr().out


def test_the_cli_refuses_with_no_overlay_instead_of_guessing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("ONEX_COMMITMENT_CHECK_OVERLAY", raising=False)
    monkeypatch.delenv("ONEX_SKILL_OVERLAY_ROOTS", raising=False)
    assert main([]) == 3
    assert "refused" in capsys.readouterr().err


def test_the_cli_fails_when_the_ledger_cannot_be_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    ledger = _backend(
        tmp_path, monkeypatch, f'{sys.executable} -c "raise SystemExit(0)"'
    )
    ledger.unlink()
    assert main([]) == 1
    assert "FAILED ledger-unreadable" in capsys.readouterr().out
