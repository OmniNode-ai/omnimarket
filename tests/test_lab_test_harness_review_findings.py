# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Review findings 5329:2, 5329:4 and 5329:5 on the central test harness (OMN-23007); each failed before the fix."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from omnimarket.handlers.emission_sink import InMemoryEmissionSink
from omnimarket.models.lab_test_harness import (
    ModelLabTestHarnessOverlay,
    ModelLabTestHarnessReading,
)
from omnimarket.nodes.node_lab_test_harness_effect import (
    runtime_lab_test_harness as runtime,
)
from omnimarket.nodes.node_lab_test_harness_effect.handlers import (
    HandlerLabTestHarnessEffect,
    InMemoryLabTestHarnessStore,
)
from omnimarket.nodes.node_lab_test_harness_effect.handlers.handler_lab_test_harness_effect import (
    FAILURE_RAISED,
    LabHarnessLedgerAppendError,
)
from omnimarket.nodes.node_lab_test_harness_effect.handlers.protocol_lab_test_harness import (
    ProtocolLabTestRunner,
)
from tests.helpers.lab_test_harness_fakes import LiveLabBus, make_origin
from tests.nodes.node_floor_simple_effect.support import NOW
from tests.test_chain_events_lab_test_harness import (
    CHAIN_NODE,
    RecordingAppender,
    of,
    overlay,
    request,
)
from tests.test_runtime_lab_test_harness import _evidence, _install_overlay

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _host(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ONEX_TEST_HOST", "h053")


@asynccontextmanager
async def _open_down(
    _overlay: ModelLabTestHarnessOverlay,
) -> AsyncIterator[ProtocolLabTestRunner]:
    raise RuntimeError("bus down")
    yield


async def test_5329_2_a_run_that_raises_publishes_its_typed_failure_row_and_keeps_its_exception(
    tmp_path: Path,
) -> None:
    _, sha = make_origin(tmp_path)
    appender = RecordingAppender()
    handler = HandlerLabTestHarnessEffect(
        overlay=overlay(sha, tmp_path),
        runner_opener=_open_down,
        store=InMemoryLabTestHarnessStore(),
        now=lambda: NOW,
        sink=InMemoryEmissionSink(),
        appender=appender,
    )
    with pytest.raises(RuntimeError, match="bus down"):
        await handler.handle(request("run-raises-row"))
    assert len(appender.rows) == 1
    row = appender.rows[0]
    assert " | verdict=NOT_WORKING" in row
    assert " | drill=no" in row
    assert f" | reason={FAILURE_RAISED}" in row


async def test_5329_2_a_ledger_that_also_fails_does_not_hide_the_original_exception(
    tmp_path: Path,
) -> None:
    class Refusing(RecordingAppender):
        def append(self, rows: str, request_id: str) -> Any:
            raise OSError("ledger down")

    _, sha = make_origin(tmp_path)
    handler = HandlerLabTestHarnessEffect(
        overlay=overlay(sha, tmp_path),
        runner_opener=_open_down,
        store=InMemoryLabTestHarnessStore(),
        now=lambda: NOW,
        sink=InMemoryEmissionSink(),
        appender=Refusing(),
    )
    with pytest.raises(RuntimeError, match="bus down"):
        await handler.handle(request("run-raises-both"))


async def test_5329_4_a_store_write_that_fails_still_closes_the_chain_with_a_failure(
    tmp_path: Path,
) -> None:
    class BrokenStore(InMemoryLabTestHarnessStore):
        def write(self, reading: ModelLabTestHarnessReading) -> None:
            raise OSError("disk full")

    url_base, sha = make_origin(tmp_path)
    sink = InMemoryEmissionSink()
    handler = HandlerLabTestHarnessEffect(
        overlay=overlay(sha, tmp_path),
        runner_opener=LiveLabBus(url_base, tmp_path / "work").opener,
        store=BrokenStore(),
        now=lambda: NOW - timedelta(minutes=5),
        sink=sink,
        appender=RecordingAppender(),
    )
    with pytest.raises(OSError, match="disk full"):
        await handler.handle(request("run-store-fails"))
    assert [e.phase for e in of(sink, CHAIN_NODE)] == ["start", "failure"]


def test_5329_5_a_ledger_that_did_not_take_the_rows_still_writes_the_timers_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _url_base, sha = make_origin(tmp_path)
    _install_overlay(tmp_path, monkeypatch, sha)

    class Unconfirmed:
        def __init__(self, **_: Any) -> None:
            return None

        async def handle(self, _request: Any) -> Any:
            raise LabHarnessLedgerAppendError("run:refused:row-grammar")

    monkeypatch.setattr(runtime, "HandlerLabTestHarnessEffect", Unconfirmed)
    state_dir = tmp_path / "evidence"
    state_dir.mkdir()
    (state_dir / "last-run.json").write_text(
        json.dumps({"run_id": "previous", "exit_code": 0}), encoding="utf-8"
    )
    code = runtime.main(["--omni-home", str(tmp_path), "--state-dir", str(state_dir)])
    assert code == runtime.EXIT_LEDGER_UNCONFIRMED
    evidence = _evidence(state_dir)
    assert evidence["run_id"] != "previous"
    assert evidence["exit_code"] == runtime.EXIT_LEDGER_UNCONFIRMED
