# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The lane receipt names the pool label it started under, and the floor's receipt reader carries it (OMN-23550).

The seam under proof joins the writer (the lane start orchestrator, whose receipt names the pool
label the engine login was drawn under) to the reader (the factory floor's ``_receipt_of``). No
token value ever appears here: only the pool label, never the token it stands for.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from omnimarket.models.model_factory_floor_input import ModelFloorReceipt
from omnimarket.nodes.node_factory_floor_effect.source_loader import (
    _receipt_of,
)
from omnimarket.nodes.node_remote_lane_start_effect.adapters.adapter_scripted_transport import (
    ModelScriptedTransportScript,
    ScriptedLaneHostTransport,
    ScriptedLoginDraw,
)
from omnimarket.nodes.node_remote_lane_start_effect.handlers.handler_remote_lane_start_effect import (
    HandlerRemoteLaneStartEffect,
)
from omnimarket.nodes.node_remote_lane_start_orchestrator.handlers.adapter_host_pool import (
    InMemoryLaneHostPool,
)
from omnimarket.nodes.node_remote_lane_start_orchestrator.models.model_remote_lane_start import (
    ModelRemoteLaneStartReceipt,
)
from omnimarket.nodes.node_remote_lane_start_orchestrator.orchestration.core import (
    FileReceiptSink,
    RemoteLaneStartOrchestrator,
)
from omnimarket.nodes.node_remote_lane_start_orchestrator.tests.lane_start_fixtures import (
    DONE_MESSAGE,
    T0,
    FixtureLedgerExecutor,
    ledger_effect,
    request,
    settings,
    snapshot,
    stream_result,
)
from omnimarket.nodes.node_work_ledger_append_effect.protocols.local_ledger_append_command import (
    LocalLedgerFile,
)

pytestmark = pytest.mark.unit


def _orchestrator(
    tmp_path: Path, login_labels: tuple[str | None, ...] | None
) -> RemoteLaneStartOrchestrator:
    """The in-memory orchestrator: the scripted transport, a ledger fixture file, a fake login draw,
    and the receipt written to ``tmp_path`` through the real ``FileReceiptSink``."""
    calls: list[str] = []
    ledger_path = tmp_path / "ledger.md"
    transport = ScriptedLaneHostTransport(
        ModelScriptedTransportScript(
            exit_polls=1, log_lines=("starting", stream_result(DONE_MESSAGE))
        ),
        calls,
    )
    executor = FixtureLedgerExecutor(ledger_path, calls)
    receipts = FileReceiptSink(str(tmp_path / "receipts"))
    return RemoteLaneStartOrchestrator(
        settings=settings(),
        host_pool=InMemoryLaneHostPool(snapshot()),
        ledger=ledger_effect(ledger_path, calls, False, executor),
        ledger_reader=LocalLedgerFile(ledger_path),
        host=HandlerRemoteLaneStartEffect(transport),
        login=None if login_labels is None else ScriptedLoginDraw(login_labels, calls),
        receipts=receipts,
        echo=lambda _line: None,
        clock=lambda: T0,
        stop_requested=lambda: False,
    )


def _receipt_file(tmp_path: Path) -> Path:
    req = request()
    return tmp_path / "receipts" / req.lane / f"{req.run_id}.json"


def _with_started_at(path: Path) -> Path:
    """Give the receipt file the ``started_at`` the floor reader needs (the writer names none)."""
    data = json.loads(path.read_text(encoding="utf-8"))
    data.setdefault("started_at", T0.isoformat())
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return path


def test_start_receipt_names_the_drawn_label(tmp_path: Path) -> None:
    orchestrator = _orchestrator(tmp_path, login_labels=("jonah-neugass",))
    orchestrator.run(request())
    data = json.loads(_receipt_file(tmp_path).read_text(encoding="utf-8"))
    assert data["login"] == "jonah-neugass"


def test_floor_reader_carries_the_label(tmp_path: Path) -> None:
    """Writer side to reader side: the label the orchestrator wrote is the one the floor reads."""
    assert "login" in ModelRemoteLaneStartReceipt.model_fields
    assert "login" in ModelFloorReceipt.model_fields
    orchestrator = _orchestrator(tmp_path, login_labels=("jonah-neugass",))
    orchestrator.run(request())
    receipt = _receipt_of(_with_started_at(_receipt_file(tmp_path)), 500)
    assert receipt is not None
    assert receipt.login == "jonah-neugass"


def test_none_draw_names_no_label(tmp_path: Path) -> None:
    """A draw that drew nothing names no label: the receipt's login is None, the reader reads blank."""
    orchestrator = _orchestrator(tmp_path, login_labels=(None,))
    orchestrator.run(request())
    path = _with_started_at(_receipt_file(tmp_path))
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["login"] is None
    receipt = _receipt_of(path, 500)
    assert receipt is not None
    assert receipt.login == ""


def test_non_label_login_is_refused() -> None:
    """A receipt login that is not a pool label is refused at validation, so a token can never ride it."""
    with pytest.raises(ValidationError):
        ModelRemoteLaneStartReceipt(
            lane="floor-alpha-a1",
            run_id="rlane-floor-alpha-a1-000001",
            ticket="OMN-22848",
            state="claimed",
            login="Bad Label!",
        )


def test_control_receipt_without_login_reads_blank(tmp_path: Path) -> None:
    """Failing control: a hand-written receipt that names no login reads blank, not as an error."""
    path = tmp_path / "handwritten.json"
    path.write_text(
        json.dumps(
            {
                "lane": "floor-alpha-a1",
                "run_id": "rlane-floor-alpha-a1-000001",
                "ticket": "OMN-22848",
                "host": "h106",
                "status": "done",
                "final": True,
                "started_at": T0.isoformat(),
            }
        )
        + "\n",
        encoding="utf-8",
    )
    receipt = _receipt_of(path, 500)
    assert receipt is not None
    assert receipt.login == ""
