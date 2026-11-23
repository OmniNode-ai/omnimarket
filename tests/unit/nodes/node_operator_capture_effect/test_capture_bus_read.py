# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The operator capture's one-shot commands read the ledger window from the ledger reader node (OMN-26330).

The failing control is the old read: a request with no ledger file path was refused (the path was required) and
the CLI refused with the path variable unset; the rows come from the reader port, and no file is opened.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from omnimarket.models.operator_capture import (
    ModelCaptureDigestRequest,
    ModelCaptureProcessRequest,
)
from omnimarket.nodes.node_operator_capture_effect import runtime_capture_cli
from omnimarket.nodes.node_operator_capture_effect.handlers import (
    capture_store,
    ledger_read,
)
from omnimarket.nodes.node_operator_capture_effect.handlers.capture_ports import (
    DelegateAnswer,
    onex_ledger_appender,
)
from omnimarket.nodes.node_operator_capture_effect.handlers.handler_capture_digest import (
    HandlerCaptureDigest,
)
from omnimarket.nodes.node_operator_capture_effect.handlers.handler_capture_process import (
    HandlerCaptureProcess,
)
from tests.helpers.fake_ledger_rows import FakeLedgerRows

pytestmark = pytest.mark.unit

NOW = datetime(2026, 10, 10, 18, 0, 0, tzinfo=UTC)
TEXT = "There should be no cloud work in M4."
ANSWER = json.dumps(
    {
        "items": [
            {
                "kind": "decision",
                "quote": TEXT,
                "subject": "cloud work m4",
                "confidence": 0.95,
            }
        ]
    }
)
RULING = (
    "2026-09-22T20:35:26Z | RULING | lane=m4-board-rescope | ticket=OMN-1 | question=Where does "
    'cloud work go? | kind=process | "No cloud work in M4; it leaves M4 for M4.5."'
)
ASK = (
    "2026-10-08T10:00:00Z | STATUS | lane=operator-capture | kind=ask | item=cap-0123456789 | "
    'ask=ask-0123456789 | state=open | session=s1 | "Fix the dashboard projection."'
)
NOISE = "2026-10-09T10:00:00Z | STATUS | lane=other | ticket=OMN-2 | nothing the capture reads"


def _delegate(prompt: str, contract: Mapping[str, Any]) -> DelegateAnswer:
    return DelegateAnswer(ANSWER, "Qwen3.8-27B", None)


def test_golden_the_digest_folds_the_asks_of_the_reader_port_with_no_ledger_file(
    tmp_path: Path,
) -> None:
    reader = FakeLedgerRows([RULING, ASK, NOISE])
    result = HandlerCaptureDigest(reader=reader).handle(
        ModelCaptureDigestRequest(store_dir=tmp_path / "store", now=NOW)
    )
    assert reader.calls == [(ledger_read.READER, ledger_read.WINDOW_HOURS, ())]
    assert result.open_asks == 1
    assert "ask-0123456789" in result.digest


def test_error_unread_a_refusing_reader_makes_the_digest_unknown_never_zero_asks(
    tmp_path: Path,
) -> None:
    result = HandlerCaptureDigest(
        reader=FakeLedgerRows([ASK], error="reader not moved")
    ).handle(ModelCaptureDigestRequest(store_dir=tmp_path / "store", now=NOW))
    assert result.digest.startswith("Open operator asks: unknown")
    assert "reader not moved" in result.digest
    assert result.open_asks == 0


def test_golden_the_drift_prior_comes_from_the_reader_port_with_no_ledger_file(
    tmp_path: Path,
) -> None:
    root = tmp_path / "store"
    assert (
        capture_store.ingest(
            root,
            session_id="sess-1",
            text=TEXT,
            source="bus:claude",
            received_at=NOW,
            origin_event="content-captured",
        )
        is not None
    )
    rows: list[str] = []

    def append(payload: str) -> tuple[bool, str]:
        rows.extend(payload.splitlines())
        return True, "ok"

    reader = FakeLedgerRows([RULING, NOISE])
    result = HandlerCaptureProcess(_delegate, append, now=NOW, reader=reader).handle(
        ModelCaptureProcessRequest(store_dir=root)
    )
    assert reader.calls == [(ledger_read.READER, ledger_read.WINDOW_HOURS, ())]
    assert result.rows_appended == 1
    assert "prior=2026-09-22T20:35:26Z" in rows[0]


def test_the_cli_digest_needs_no_ledger_path_variable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # An environment holding only the capture store: no ledger file path variable of any name.
    monkeypatch.setattr(
        os, "environ", {"ONEX_OPERATOR_CAPTURE_DIR": str(tmp_path / "store")}
    )
    monkeypatch.setattr(
        ledger_read,
        "SubprocessLedgerRowsReader",
        lambda _environ, **_kw: FakeLedgerRows([ASK]),
    )
    result = CliRunner().invoke(runtime_capture_cli.operator_capture_group, ["digest"])
    assert result.exit_code == 0, result.output
    assert "ask-0123456789" in result.output


def test_the_ledger_appender_needs_no_ledger_path_variable() -> None:
    ok, detail = onex_ledger_appender({"ONEX_OPERATOR_CAPTURE_LEDGER_BIN": "true"})(
        "row"
    )
    assert ok, detail
