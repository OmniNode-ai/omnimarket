# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Ledger-host safety and redelivery proofs (OMN-20275)."""

from datetime import UTC, datetime
from importlib import resources
from pathlib import Path
from uuid import UUID, uuid4

import pytest
import yaml
from pydantic import ValidationError

from omnimarket.nodes.node_work_ledger_append_effect import (
    EnumWorkLedgerAppendStatus,
    HandlerWorkLedgerAppendEffect,
    ModelWorkLedgerAppendRequest,
)
from omnimarket.nodes.node_work_ledger_append_effect.protocols import (
    LocalLedgerFile,
    ModelAppendCommandResult,
)
from omnimarket.work_ledger_bus.bus import load_work_ledger_append_topics

pytestmark = pytest.mark.unit


class _Runner:
    def __init__(
        self, path: Path, codes: list[int] | None = None, stderr: str = ""
    ) -> None:
        self.path = path
        self.codes = iter(codes or [0])
        self.stderr = stderr
        self.calls: list[str] = []

    def append(self, rows: str) -> ModelAppendCommandResult:
        self.calls.append(rows)
        code = next(self.codes)
        if code == 0:
            with self.path.open("a", encoding="utf-8") as stream:
                stream.write(rows + "\n")
        return ModelAppendCommandResult(exit_code=code, stderr=self.stderr)


def _row(request_id: UUID, row_type: str = "STATUS") -> str:
    return (
        f"2026-10-01T12:00:00Z | {row_type} | lane=test | req={request_id} | exact text"
    )


def _request(
    rows: str | None = None, request_id: UUID | None = None
) -> ModelWorkLedgerAppendRequest:
    request_id = request_id or uuid4()
    return ModelWorkLedgerAppendRequest(
        request_id=request_id,
        rows=rows if rows is not None else _row(request_id),
        requested_by_lane="lab",
        requesting_host="lab-host",
        requested_at=datetime.now(UTC),
    )


def _ledger(tmp_path: Path) -> Path:
    path = tmp_path / "ledger.md"
    path.write_text("# Rolling ledger\n", encoding="utf-8")
    return path


def test_redelivered_request_appends_once(tmp_path: Path) -> None:
    path = _ledger(tmp_path)
    runner = _Runner(path)
    handler = HandlerWorkLedgerAppendEffect(
        runner, LocalLedgerFile(path), "ledger-host"
    )
    request = _request()
    first = handler.handle(request)
    second = handler.handle(request)
    assert first.status is EnumWorkLedgerAppendStatus.ACCEPTED
    assert second.status is EnumWorkLedgerAppendStatus.DUPLICATE
    assert first.exit_code == second.exit_code == 0
    assert first.ledger_lines == second.ledger_lines == [2]
    assert first.ledger_host == "ledger-host"
    assert first.duration_ms >= 0
    assert runner.calls == [request.rows]
    assert path.read_text().count(request.rows) == 1


def test_refused_append_leaves_ledger_unchanged(tmp_path: Path) -> None:
    path = _ledger(tmp_path)
    before = path.read_bytes()
    runner = _Runner(path, [65], "missing required field; remedy: supply lane=")
    receipt = HandlerWorkLedgerAppendEffect(
        runner, LocalLedgerFile(path), "ledger"
    ).handle(_request())
    assert receipt.status is EnumWorkLedgerAppendStatus.REFUSED
    assert receipt.exit_code == 65
    assert receipt.message == runner.stderr
    assert receipt.ledger_lines == []
    assert path.read_bytes() == before
    assert len(runner.calls) == 1


@pytest.mark.parametrize("row_type", ["RULING", "OPERATOR-CONSENT"])
def test_consent_and_ruling_refused_over_bus(tmp_path: Path, row_type: str) -> None:
    path = _ledger(tmp_path)
    runner = _Runner(path)
    request_id = uuid4()
    # Even a previously landed request cannot bypass the type refusal.
    rows = _row(request_id, row_type)
    path.write_text(rows + "\n")
    receipt = HandlerWorkLedgerAppendEffect(
        runner, LocalLedgerFile(path), "ledger"
    ).handle(_request(rows, request_id))
    assert receipt.status is EnumWorkLedgerAppendStatus.REFUSED
    assert receipt.exit_code == 65
    assert receipt.message == (
        "RULING and OPERATOR-CONSENT rows are not accepted over the bus until the "
        "receipt can name an authenticated principal (OMN-20275)"
    )
    assert runner.calls == []


@pytest.mark.parametrize(
    "bad_cell", ["", "req=other", "req={id}suffix", "text req={id}", "req={id} more"]
)
def test_missing_req_cell_refuses_the_named_row(tmp_path: Path, bad_cell: str) -> None:
    path = _ledger(tmp_path)
    runner = _Runner(path)
    request_id = uuid4()
    rows = (
        _row(request_id)
        + "\n"
        + f"2026-10-01T12:00:01Z | MSG | {bad_cell.format(id=request_id)} | text"
    )
    receipt = HandlerWorkLedgerAppendEffect(
        runner, LocalLedgerFile(path), "ledger"
    ).handle(_request(rows, request_id))
    assert receipt.status is EnumWorkLedgerAppendStatus.REFUSED
    assert receipt.exit_code == 65
    assert "row 2 (MSG)" in receipt.message
    assert runner.calls == []


@pytest.mark.parametrize("rows", ["not a row", "2026-10-01T12:00:00Z |NO| nope"])
def test_no_ledger_row_refused(tmp_path: Path, rows: str) -> None:
    path = _ledger(tmp_path)
    runner = _Runner(path)
    receipt = HandlerWorkLedgerAppendEffect(
        runner, LocalLedgerFile(path), "ledger"
    ).handle(_request(rows))
    assert receipt.status is EnumWorkLedgerAppendStatus.REFUSED
    assert receipt.exit_code == 65
    assert "no ledger row" in receipt.message
    assert runner.calls == []


def test_unknown_row_type_refused(tmp_path: Path) -> None:
    path = _ledger(tmp_path)
    runner = _Runner(path)
    request_id = uuid4()
    receipt = HandlerWorkLedgerAppendEffect(
        runner, LocalLedgerFile(path), "ledger"
    ).handle(_request(_row(request_id, "UNKNOWN"), request_id))
    assert receipt.exit_code == 65
    assert "OMN-20275" in receipt.message
    assert runner.calls == []


@pytest.mark.parametrize("ending", ["", " |", " | more"])
def test_multiple_rows_continuations_and_whole_cell_dedup(
    tmp_path: Path, ending: str
) -> None:
    path = _ledger(tmp_path)
    request_id = uuid4()
    # Prefixes, prose, and different ids must not trigger a duplicate.
    path.write_text(
        f"# Ledger\ntext req={request_id}\nx | req={request_id}suffix\nx | req={uuid4()}\n"
    )
    rows = (
        f"2026-10-01T12:00:00Z | MSG | req={request_id}{ending}\n  continuation stays exact\n"
        + _row(request_id)
    )
    runner = _Runner(path)
    handler = HandlerWorkLedgerAppendEffect(runner, LocalLedgerFile(path), "ledger")
    first = handler.handle(_request(rows, request_id))
    second = handler.handle(_request(rows, request_id))
    assert first.status is EnumWorkLedgerAppendStatus.ACCEPTED
    assert second.status is EnumWorkLedgerAppendStatus.DUPLICATE
    assert first.ledger_lines == second.ledger_lines == [5, 7]
    assert runner.calls == [rows]


@pytest.mark.parametrize(
    ("codes", "expected"), [([75, 75, 0], "accepted"), ([75, 75, 75], "error")]
)
def test_lock_timeout_retry(tmp_path: Path, codes: list[int], expected: str) -> None:
    path = _ledger(tmp_path)
    runner = _Runner(path, codes, "lock timeout")
    sleeps: list[float] = []
    handler = HandlerWorkLedgerAppendEffect(
        runner, LocalLedgerFile(path), "ledger", retry_sleep_s=0, sleep=sleeps.append
    )
    receipt = handler.handle(_request())
    assert receipt.status.value == expected
    assert receipt.exit_code == codes[-1]
    assert len(runner.calls) == 3
    assert sleeps == [0, 0]


@pytest.mark.parametrize("code", [1, 70, 124])
def test_other_exit_is_error_with_bounded_tail(tmp_path: Path, code: int) -> None:
    path = _ledger(tmp_path)
    runner = _Runner(path, [code], "x" * 2100 + "final output")
    receipt = HandlerWorkLedgerAppendEffect(
        runner, LocalLedgerFile(path), "ledger"
    ).handle(_request())
    assert receipt.status is EnumWorkLedgerAppendStatus.ERROR
    assert receipt.exit_code == code
    assert receipt.message.endswith("final output")
    assert len(receipt.message) == 2000
    assert len(runner.calls) == 1


def test_exit_zero_with_dedup_stderr_is_accepted(tmp_path: Path) -> None:
    path = _ledger(tmp_path)
    runner = _Runner(path, [0], "DEDUP: already landed during append")
    receipt = HandlerWorkLedgerAppendEffect(
        runner, LocalLedgerFile(path), "ledger"
    ).handle(_request())
    assert receipt.status is EnumWorkLedgerAppendStatus.ACCEPTED
    assert receipt.message.startswith("DEDUP")
    assert receipt.ledger_lines == [2]


def test_contract_declares_no_event_bus_and_all_three_topics() -> None:
    contract = yaml.safe_load(
        resources.files("omnimarket.nodes.node_work_ledger_append_effect")
        .joinpath("contract.yaml")
        .read_text()
    )
    assert "event_bus" not in contract
    assert contract["node_type"] == "EFFECT_GENERIC"
    topics = load_work_ledger_append_topics()
    assert topics.command == "onex.cmd.omnimarket.work-ledger-append-requested.v1"
    assert topics.success == "onex.evt.omnimarket.work-ledger-append-completed.v1"
    assert topics.failure == "onex.evt.omnimarket.work-ledger-append-failed.v1"


def test_request_requires_aware_time_and_bounded_rows() -> None:
    payload = _request().model_dump()
    for changes in (
        {"requested_at": datetime(2026, 10, 1)},
        {"rows": ""},
        {"rows": "x" * 65537},
        {"unknown": True},
    ):
        with pytest.raises(ValidationError):
            ModelWorkLedgerAppendRequest.model_validate(payload | changes)
    with pytest.raises(ValidationError):
        _request().rows = "mutated"


def test_partial_append_is_an_error_not_a_duplicate(tmp_path: Path) -> None:
    """Two rows share one req= cell; only one is on the ledger (model review)."""
    path = _ledger(tmp_path)
    request_id = uuid4()
    first = f"2026-10-01T00:00:00Z | STATUS | lane=a | req={request_id} | one"
    second = f"2026-10-01T00:00:01Z | STATUS | lane=a | req={request_id} | two"
    path.write_text(path.read_text(encoding="utf-8") + first + "\n", encoding="utf-8")
    calls: list[str] = []

    class Runner:
        def append(self, rows: str) -> ModelAppendCommandResult:
            calls.append(rows)
            return ModelAppendCommandResult(exit_code=0)

    handler = HandlerWorkLedgerAppendEffect(
        Runner(), LocalLedgerFile(path), "ledger-host"
    )
    request = ModelWorkLedgerAppendRequest(
        request_id=request_id,
        rows=f"{first}\n{second}\n",
        requested_by_lane="a",
        requesting_host="h",
        requested_at=datetime.now(UTC),
    )
    receipt = handler.handle(request)
    assert receipt.status is EnumWorkLedgerAppendStatus.ERROR
    assert "partial append: 1 of 2" in receipt.message
    assert calls == []


def test_rows_landed_despite_nonzero_exit_are_accepted(tmp_path: Path) -> None:
    path = _ledger(tmp_path)
    request_id = uuid4()
    row = f"2026-10-01T00:00:00Z | STATUS | lane=a | req={request_id} | one"

    class Runner:
        def append(self, rows: str) -> ModelAppendCommandResult:
            path.write_text(path.read_text(encoding="utf-8") + rows, encoding="utf-8")
            return ModelAppendCommandResult(exit_code=78, stderr="stranded clone")

    handler = HandlerWorkLedgerAppendEffect(
        Runner(), LocalLedgerFile(path), "ledger-host"
    )
    receipt = handler.handle(
        ModelWorkLedgerAppendRequest(
            request_id=request_id,
            rows=row + "\n",
            requested_by_lane="a",
            requesting_host="h",
            requested_at=datetime.now(UTC),
        )
    )
    assert receipt.status is EnumWorkLedgerAppendStatus.ACCEPTED
    assert receipt.exit_code == 0
    assert "stranded clone" in receipt.message
