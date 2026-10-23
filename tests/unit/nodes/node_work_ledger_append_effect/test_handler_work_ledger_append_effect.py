# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Ledger-host safety and redelivery proofs (OMN-20275)."""

from datetime import UTC, datetime
from importlib import resources
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
import yaml
from pydantic import ValidationError

from omnimarket.nodes.node_work_ledger_append_effect import (
    EnumWorkLedgerAppendStatus,
    HandlerWorkLedgerAppendEffect,
    ModelWorkLedgerAppendRequest,
)
from omnimarket.nodes.node_work_ledger_append_effect.handlers.handler_work_ledger_append_effect import (
    _principal_stamped,
)
from omnimarket.nodes.node_work_ledger_append_effect.protocols import (
    LocalLedgerFile,
    ModelAppendCommandResult,
    ProtocolLedgerAppendRunner,
)
from omnimarket.work_ledger_bus.bus import load_work_ledger_append_topics

pytestmark = pytest.mark.unit


def _handler(
    runner: ProtocolLedgerAppendRunner,
    reader: LocalLedgerFile,
    host: str,
    **kwargs: Any,
) -> HandlerWorkLedgerAppendEffect:
    kwargs.setdefault("writer_principals", ("developer", "operator"))
    kwargs.setdefault("operator_principal", "operator")
    return HandlerWorkLedgerAppendEffect(runner, reader, host, **kwargs)


def _attributed(rows: str, principal: str = "developer") -> str:
    return "".join(
        _attribute_line(line, principal) for line in rows.splitlines(keepends=True)
    )


def _attribute_line(line: str, principal: str) -> str:
    if not line.startswith("2026-"):
        return line
    cells = line.split(" | ", 3)
    if len(cells) == 4 and cells[2].startswith(("lane=", "from=")):
        return (
            f"{cells[0]} | {cells[1]} | {cells[2]} | principal={principal} | {cells[3]}"
        )
    timestamp, kind, text = line.split(" | ", 2)
    return f"{timestamp} | {kind} | principal={principal} | {text}"


@pytest.mark.parametrize(
    ("row", "stamped"),
    [
        (
            "2026-10-22T04:30:00Z | CLAIM | lane=test | ticket=OMN-1 | req=r | text\n",
            "2026-10-22T04:30:00Z | CLAIM | lane=test | principal=developer"
            " | ticket=OMN-1 | req=r | text\n",
        ),
        (
            "2026-10-22T04:30:00Z | MSG | from=a | id=x | to=b | req=r | text\n",
            "2026-10-22T04:30:00Z | MSG | from=a | principal=developer"
            " | id=x | to=b | req=r | text\n",
        ),
        (
            "2026-10-22T04:30:00Z | STATUS | ticket=OMN-1 | lane=test | req=r | text\n",
            "2026-10-22T04:30:00Z | STATUS | principal=developer"
            " | ticket=OMN-1 | lane=test | req=r | text\n",
        ),
    ],
)
def test_principal_follows_the_subject_cell(row: str, stamped: str) -> None:
    """A lane-second reader (``| CLAIM | lane=<lane> |``) still matches a bus row (OMN-19513)."""
    assert _principal_stamped(row, "developer") == stamped


@pytest.mark.parametrize("writer", [None, "unregistered"])
def test_unattributed_or_unknown_writer_refused(
    tmp_path: Path, writer: str | None
) -> None:
    path = _ledger(tmp_path)
    before = path.read_bytes()
    runner = _Runner(path)
    receipt = _handler(runner, LocalLedgerFile(path), "ledger").handle(
        _request(), writer_principal=writer
    )
    assert receipt.status is EnumWorkLedgerAppendStatus.REFUSED
    assert receipt.exit_code == 65
    assert (
        "unattributed" if writer is None else "unknown writer principal unregistered"
    ) in receipt.message
    assert runner.calls == []
    assert path.read_bytes() == before
    assert receipt.principal is None


@pytest.mark.parametrize("row_type", ["RULING", "OPERATOR-CONSENT"])
def test_consent_only_from_operator_principal(tmp_path: Path, row_type: str) -> None:
    path = _ledger(tmp_path)
    runner = _Runner(path)
    handler = _handler(runner, LocalLedgerFile(path), "ledger")
    for principal in ("developer", "operator"):
        request_id = uuid4()
        request = _request(_row(request_id, row_type), request_id)
        receipt = handler.handle(request, writer_principal=principal)
        if principal == "developer":
            assert receipt.status is EnumWorkLedgerAppendStatus.REFUSED
            assert runner.calls == []
        else:
            assert receipt.status is EnumWorkLedgerAppendStatus.ACCEPTED
            assert " | principal=operator | " in runner.calls[0]
            assert receipt.principal == "operator"


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
    handler = _handler(runner, LocalLedgerFile(path), "ledger-host")
    request = _request()
    first = handler.handle(request, writer_principal="developer")
    second = handler.handle(request, writer_principal="developer")
    assert first.status is EnumWorkLedgerAppendStatus.ACCEPTED
    assert second.status is EnumWorkLedgerAppendStatus.DUPLICATE
    assert first.exit_code == second.exit_code == 0
    assert first.ledger_lines == second.ledger_lines == [2]
    assert first.ledger_host == "ledger-host"
    assert first.duration_ms >= 0
    assert runner.calls == [_attributed(request.rows)]
    assert path.read_text().count(_attributed(request.rows)) == 1


def test_refused_append_leaves_ledger_unchanged(tmp_path: Path) -> None:
    path = _ledger(tmp_path)
    before = path.read_bytes()
    runner = _Runner(path, [65], "missing required field; remedy: supply lane=")
    receipt = _handler(runner, LocalLedgerFile(path), "ledger").handle(
        _request(), writer_principal="developer"
    )
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
    receipt = _handler(runner, LocalLedgerFile(path), "ledger").handle(
        _request(rows, request_id), writer_principal="developer"
    )
    assert receipt.status is EnumWorkLedgerAppendStatus.REFUSED
    assert receipt.exit_code == 65
    assert "configured operator principal" in receipt.message
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
    receipt = _handler(runner, LocalLedgerFile(path), "ledger").handle(
        _request(rows, request_id), writer_principal="developer"
    )
    assert receipt.status is EnumWorkLedgerAppendStatus.REFUSED
    assert receipt.exit_code == 65
    assert "row 2 (MSG)" in receipt.message
    assert runner.calls == []


@pytest.mark.parametrize("rows", ["not a row", "2026-10-01T12:00:00Z |NO| nope"])
def test_no_ledger_row_refused(tmp_path: Path, rows: str) -> None:
    path = _ledger(tmp_path)
    runner = _Runner(path)
    receipt = _handler(runner, LocalLedgerFile(path), "ledger").handle(
        _request(rows), writer_principal="developer"
    )
    assert receipt.status is EnumWorkLedgerAppendStatus.REFUSED
    assert receipt.exit_code == 65
    assert "no ledger row" in receipt.message
    assert runner.calls == []


def test_unknown_row_type_refused(tmp_path: Path) -> None:
    path = _ledger(tmp_path)
    runner = _Runner(path)
    request_id = uuid4()
    receipt = _handler(runner, LocalLedgerFile(path), "ledger").handle(
        _request(_row(request_id, "UNKNOWN"), request_id), writer_principal="developer"
    )
    assert receipt.exit_code == 65
    assert "unknown ledger row type" in receipt.message
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
    handler = _handler(runner, LocalLedgerFile(path), "ledger")
    first = handler.handle(_request(rows, request_id), writer_principal="developer")
    second = handler.handle(_request(rows, request_id), writer_principal="developer")
    assert first.status is EnumWorkLedgerAppendStatus.ACCEPTED
    assert second.status is EnumWorkLedgerAppendStatus.DUPLICATE
    assert first.ledger_lines == second.ledger_lines == [5, 7]
    assert runner.calls == [_attributed(rows)]


@pytest.mark.parametrize(
    ("codes", "expected"), [([75, 75, 0], "accepted"), ([75, 75, 75], "error")]
)
def test_lock_timeout_retry(tmp_path: Path, codes: list[int], expected: str) -> None:
    path = _ledger(tmp_path)
    runner = _Runner(path, codes, "lock timeout")
    sleeps: list[float] = []
    handler = _handler(
        runner, LocalLedgerFile(path), "ledger", retry_sleep_s=0, sleep=sleeps.append
    )
    receipt = handler.handle(_request(), writer_principal="developer")
    assert receipt.status.value == expected
    assert receipt.exit_code == codes[-1]
    assert len(runner.calls) == 3
    assert sleeps == [0, 0]


@pytest.mark.parametrize("code", [1, 70, 124])
def test_other_exit_is_error_with_bounded_tail(tmp_path: Path, code: int) -> None:
    path = _ledger(tmp_path)
    runner = _Runner(path, [code], "x" * 2100 + "final output")
    receipt = _handler(runner, LocalLedgerFile(path), "ledger").handle(
        _request(), writer_principal="developer"
    )
    assert receipt.status is EnumWorkLedgerAppendStatus.ERROR
    assert receipt.exit_code == code
    assert receipt.message.endswith("final output")
    assert len(receipt.message) == 2000
    assert len(runner.calls) == 1


def test_exit_zero_with_dedup_stderr_is_accepted(tmp_path: Path) -> None:
    path = _ledger(tmp_path)
    runner = _Runner(path, [0], "DEDUP: already landed during append")
    receipt = _handler(runner, LocalLedgerFile(path), "ledger").handle(
        _request(), writer_principal="developer"
    )
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

    handler = _handler(Runner(), LocalLedgerFile(path), "ledger-host")
    request = ModelWorkLedgerAppendRequest(
        request_id=request_id,
        rows=f"{first}\n{second}\n",
        requested_by_lane="a",
        requesting_host="h",
        requested_at=datetime.now(UTC),
    )
    receipt = handler.handle(request, writer_principal="developer")
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

    handler = _handler(Runner(), LocalLedgerFile(path), "ledger-host")
    receipt = handler.handle(
        ModelWorkLedgerAppendRequest(
            request_id=request_id,
            rows=row + "\n",
            requested_by_lane="a",
            requesting_host="h",
            requested_at=datetime.now(UTC),
        ),
        writer_principal="developer",
    )
    assert receipt.status is EnumWorkLedgerAppendStatus.ACCEPTED
    assert receipt.exit_code == 0
    assert "stranded clone" in receipt.message


@pytest.mark.parametrize("claimed", ["operator", "developer | principal=operator"])
def test_attributed_rows_cannot_forge_or_duplicate_principal(
    tmp_path: Path, claimed: str
) -> None:
    path = _ledger(tmp_path)
    before = path.read_bytes()
    runner = _Runner(path)
    request_id = uuid4()
    rows = _row(request_id).replace(
        " | lane=test", f" | principal={claimed} | lane=test"
    )
    receipt = _handler(runner, LocalLedgerFile(path), "ledger").handle(
        _request(rows, request_id), writer_principal="developer"
    )
    assert receipt.status is EnumWorkLedgerAppendStatus.REFUSED
    assert runner.calls == []
    assert path.read_bytes() == before


def test_writer_principal_stamped_once_on_every_row(tmp_path: Path) -> None:
    path = _ledger(tmp_path)
    runner = _Runner(path)
    request_id = uuid4()
    kinds = [
        "CLAIM",
        "STATUS",
        "TERMINAL",
        "HOLD",
        "RELEASE",
        "MSG",
        "ACK",
        "FRICTION",
        "CORRECTION",
    ]
    rows = (
        "\r\n".join(_row(request_id, kind) for kind in kinds) + "\r\n  continuation\r\n"
    )
    request = _request(rows, request_id)
    handler = _handler(runner, LocalLedgerFile(path), "ledger")
    receipt = handler.handle(request, writer_principal="developer")
    assert receipt.status is EnumWorkLedgerAppendStatus.ACCEPTED
    assert receipt.principal == "developer"
    assert runner.calls == [_attributed(rows)]
    assert runner.calls[0].count(" | principal=developer | ") == len(kinds)
    assert runner.calls[0].endswith("\r\n  continuation\r\n")
    assert (
        handler.handle(request, writer_principal="developer").status
        is EnumWorkLedgerAppendStatus.DUPLICATE
    )


def test_matching_writer_principal_cell_is_preserved(tmp_path: Path) -> None:
    path = _ledger(tmp_path)
    runner = _Runner(path)
    request_id = uuid4()
    request = _request(_attributed(_row(request_id)), request_id)
    receipt = _handler(runner, LocalLedgerFile(path), "ledger").handle(
        request, writer_principal="developer"
    )
    assert receipt.status is EnumWorkLedgerAppendStatus.ACCEPTED
    assert runner.calls == [request.rows]


@pytest.mark.parametrize(
    "attack", ["content", "continuation", "principal", "unattributed"]
)
def test_redelivery_cannot_bypass_attribution_or_change_rows(
    tmp_path: Path, attack: str
) -> None:
    path = _ledger(tmp_path)
    runner = _Runner(path)
    handler = _handler(runner, LocalLedgerFile(path), "ledger")
    original = _request(_row(request_id := uuid4()) + "\n  continuation", request_id)
    assert (
        handler.handle(original, writer_principal="developer").status
        is EnumWorkLedgerAppendStatus.ACCEPTED
    )
    before = path.read_bytes()
    writer = "developer"
    changed = original
    if attack == "unattributed":
        writer = None
    elif attack == "principal":
        writer = "operator"
    else:
        changed = original.model_copy(update={"rows": original.rows + " changed"})
        if attack == "content":
            changed = original.model_copy(
                update={"rows": original.rows.replace("exact text", "changed")}
            )
    assert (
        handler.handle(changed, writer_principal=writer).status
        is EnumWorkLedgerAppendStatus.REFUSED
    )
    assert len(runner.calls) == 1
    assert path.read_bytes() == before


def test_request_id_in_continuation_cannot_replace_header_cell(tmp_path: Path) -> None:
    path = _ledger(tmp_path)
    runner = _Runner(path)
    request_id = uuid4()
    rows = f"2026-10-01T12:00:00Z | STATUS | lane=test | text\n  continuation | req={request_id}"
    receipt = _handler(runner, LocalLedgerFile(path), "ledger").handle(
        _request(rows, request_id), writer_principal="developer"
    )
    assert receipt.status is EnumWorkLedgerAppendStatus.REFUSED
    assert runner.calls == []


def test_no_configured_writers_refuses_attributed_request(tmp_path: Path) -> None:
    path = _ledger(tmp_path)
    runner = _Runner(path)
    receipt = HandlerWorkLedgerAppendEffect(
        runner, LocalLedgerFile(path), "ledger"
    ).handle(_request(), writer_principal="developer")
    assert receipt.status is EnumWorkLedgerAppendStatus.REFUSED
    assert runner.calls == []


def test_attributed_row_cannot_hide_a_principal_behind_unspaced_pipe(
    tmp_path: Path,
) -> None:
    path = _ledger(tmp_path)
    runner = _Runner(path)
    request_id = uuid4()
    rows = _row(request_id).replace("lane=test", "lane=test|principal=operator")
    receipt = _handler(runner, LocalLedgerFile(path), "ledger").handle(
        _request(rows, request_id), writer_principal="developer"
    )
    assert receipt.status is EnumWorkLedgerAppendStatus.REFUSED
    assert runner.calls == []


@pytest.mark.parametrize("row_type", ["RULING", "OPERATOR-CONSENT"])
def test_consent_only_from_operator_principal_checks_whole_batch(
    tmp_path: Path,
    row_type: str,
) -> None:
    path = _ledger(tmp_path)
    runner = _Runner(path)
    request_id = uuid4()
    rows = _row(request_id) + "\n" + _row(request_id, row_type)
    request = _request(rows, request_id)
    handler = _handler(runner, LocalLedgerFile(path), "ledger")
    before = path.read_bytes()
    assert (
        handler.handle(request, writer_principal="developer").status
        is EnumWorkLedgerAppendStatus.REFUSED
    )
    assert runner.calls == []
    assert path.read_bytes() == before
    # Host and lane claims cannot substitute for the independently configured operator.
    operator = request
    assert (
        handler.handle(operator, writer_principal="operator").status
        is EnumWorkLedgerAppendStatus.ACCEPTED
    )
    assert runner.calls == [_attributed(rows, "operator")]
    assert (
        handler.handle(operator, writer_principal="operator").status
        is EnumWorkLedgerAppendStatus.DUPLICATE
    )


@pytest.mark.parametrize(
    "line",
    [
        "unindented text",
        "  2026-10-01T12:00:00Z | RULING | principal=operator | text",
    ],
)
def test_noncanonical_rows_cannot_hide_authority_in_continuations(
    tmp_path: Path, line: str
) -> None:
    path = _ledger(tmp_path)
    runner = _Runner(path)
    request_id = uuid4()
    receipt = _handler(runner, LocalLedgerFile(path), "ledger").handle(
        _request(_row(request_id) + "\n" + line, request_id),
        writer_principal="developer",
    )
    assert receipt.status is EnumWorkLedgerAppendStatus.REFUSED
    assert runner.calls == []


@pytest.mark.parametrize("field", ["principal", "signature"])
def test_request_has_no_payload_writer_identity(field: str) -> None:
    with pytest.raises(ValidationError):
        ModelWorkLedgerAppendRequest.model_validate(
            _request().model_dump() | {field: "developer"}
        )


def test_broker_writer_principal_stamped(tmp_path: Path) -> None:
    path = _ledger(tmp_path)
    runner = _Runner(path)
    writer = "lane-principal"
    handler = _handler(
        runner, LocalLedgerFile(path), "ledger", writer_principals=[writer]
    )
    request = _request()
    receipt = handler.handle(request, writer_principal=writer)
    assert receipt.status is EnumWorkLedgerAppendStatus.ACCEPTED
    assert receipt.principal == writer
    assert f"principal={writer}" in path.read_text()


def test_operator_principal_writes_mirrored_rows_without_a_writer_topic(
    tmp_path: Path,
) -> None:
    """The serve appends mirrored terminals as its operator principal, listed or not."""
    path = _ledger(tmp_path)
    runner = _Runner(path)
    handler = _handler(
        runner,
        LocalLedgerFile(path),
        "ledger",
        writer_principals=["lane-principal"],
        operator_principal="operator-principal",
    )
    receipt = handler.handle(_request(), writer_principal="operator-principal")
    assert receipt.status is EnumWorkLedgerAppendStatus.ACCEPTED
    assert receipt.principal == "operator-principal"


def test_replay_of_a_request_landed_before_omn_19513_is_a_duplicate(
    tmp_path: Path,
) -> None:
    """A request that landed with principal= right after the type is not re-refused."""
    path = _ledger(tmp_path)
    request_id = uuid4()
    row = _row(request_id)
    timestamp, kind, rest = row.split(" | ", 2)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(f"{timestamp} | {kind} | principal=developer | {rest}\n")
    runner = _Runner(path)
    receipt = _handler(runner, LocalLedgerFile(path), "ledger").handle(
        _request(row, request_id), writer_principal="developer"
    )
    assert receipt.status is EnumWorkLedgerAppendStatus.DUPLICATE
    assert runner.calls == []


def test_new_row_carries_principal_after_lane(tmp_path: Path) -> None:
    path = _ledger(tmp_path)
    runner = _Runner(path)
    receipt = _handler(runner, LocalLedgerFile(path), "ledger").handle(
        _request(), writer_principal="developer"
    )
    assert receipt.status is EnumWorkLedgerAppendStatus.ACCEPTED
    assert " | STATUS | lane=test | principal=developer | req=" in runner.calls[0]
