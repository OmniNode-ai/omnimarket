# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The node runs from the command line with a supplied sender, and the publisher adapter builds one from argv."""

from __future__ import annotations

import json
import stat
import sys
from pathlib import Path

import pytest

from omnimarket.nodes.node_ledger_outbox_settle_effect.__main__ import main
from omnimarket.nodes.node_ledger_outbox_settle_effect.adapters.adapter_publisher_sender import (
    PublisherSender,
)

from .test_outbox_settle import REQ, Publisher, refused_request, stamp

pytestmark = pytest.mark.unit


def _request_json(ledger: Path) -> str:
    return json.dumps({"ledger_path": str(ledger)})


def test_main_runs_the_node_with_a_supplied_sender(
    ledger: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    refused_request(
        ledger, f"{stamp()} | STATUS | lane=a | req={REQ} | a row\n", REQ, "refused"
    )
    code = main(["--input", _request_json(ledger)], sender=Publisher(["accepted"]))
    assert code == 0
    out = json.loads(capsys.readouterr().out)
    assert (out["settled"], out["remaining"], out["stopped"]) == (1, 0, "")


def test_main_exits_one_when_the_run_stops(
    ledger: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    refused_request(
        ledger, f"{stamp()} | STATUS | lane=a | req={REQ} | a row\n", REQ, "refused"
    )
    code = main(["--input", _request_json(ledger)], sender=Publisher(["pending"]))
    assert code == 1
    assert "exit 75" in capsys.readouterr().err


def test_main_without_a_publisher_is_indeterminate(
    ledger: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = main(["--input", _request_json(ledger)])
    assert code == 3
    assert "INDETERMINATE" in capsys.readouterr().err


def test_publisher_sender_reads_the_receipt_of_the_publisher_process(
    ledger: Path, tmp_path: Path
) -> None:
    script = tmp_path / "publisher.py"
    script.write_text(
        "import json, sys\n"
        "args = sys.argv[1:]\n"
        "rid = args[args.index('--request-id') + 1]\n"
        "sys.stdin.read()\n"
        "print(json.dumps({'status': 'accepted', 'request_id': rid}))\n"
    )
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    sender = PublisherSender([sys.executable, str(script)], lane="a", host="h")
    result = sender("row\n", REQ)
    assert (result.exit_code, result.receipt) == (
        0,
        {"status": "accepted", "request_id": REQ},
    )


def test_publisher_sender_answers_pending_when_the_publisher_cannot_start() -> None:
    result = PublisherSender(["/nonexistent/publisher"], lane="a", host="h")(
        "row\n", REQ
    )
    assert result.exit_code == 75
