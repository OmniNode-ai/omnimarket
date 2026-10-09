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
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from pydantic import ValidationError

from omnimarket.nodes.node_work_ledger_append_effect import (
    EnumWorkLedgerAppendStatus,
    HandlerWorkLedgerAppendEffect,
    ModelWorkLedgerAppendRequest,
)
from omnimarket.nodes.node_work_ledger_append_effect.protocols import (
    LocalLedgerFile,
    ModelAppendCommandResult,
    ProtocolLedgerAppendRunner,
)
from omnimarket.work_ledger_bus.bus import load_work_ledger_append_topics

pytestmark = pytest.mark.unit

_DEVELOPER_KEY = Ed25519PrivateKey.generate()
_OPERATOR_KEY = Ed25519PrivateKey.generate()


def _handler(
    runner: ProtocolLedgerAppendRunner,
    reader: LocalLedgerFile,
    host: str,
    **kwargs: Any,
) -> HandlerWorkLedgerAppendEffect:
    kwargs.setdefault(
        "public_keys",
        {
            "developer": _DEVELOPER_KEY.public_key(),
            "operator": _OPERATOR_KEY.public_key(),
        },
    )
    kwargs.setdefault("operator_principal", "operator")
    return HandlerWorkLedgerAppendEffect(runner, reader, host, **kwargs)


def _attributed(rows: str, principal: str = "developer") -> str:
    return "".join(
        _attribute_line(line, principal) for line in rows.splitlines(keepends=True)
    )


def _attribute_line(line: str, principal: str) -> str:
    if not line.startswith("2026-"):
        return line
    timestamp, kind, text = line.split(" | ", 2)
    return f"{timestamp} | {kind} | principal={principal} | {text}"


@pytest.mark.parametrize(
    "attack",
    [
        "unsigned",
        "unknown",
        "wrong-key",
        "malformed",
        "rows",
        "request_id",
        "ledger_id",
        "requested_by_lane",
        "requesting_host",
        "requested_at",
        "principal",
    ],
)
def test_unsigned_or_forged_request_refused(tmp_path: Path, attack: str) -> None:
    path = _ledger(tmp_path)
    before = path.read_bytes()
    runner = _Runner(path)
    request = _request()
    if attack == "unsigned":
        request = request.model_copy(update={"signature": None})
    elif attack == "unknown":
        request = request.signed("unregistered", _DEVELOPER_KEY)
    elif attack == "wrong-key":
        request = request.signed("developer", _OPERATOR_KEY)
    else:
        changes = {
            "malformed": {"signature": "not-base64"},
            "rows": {"rows": request.rows + " altered"},
            "request_id": {"request_id": uuid4()},
            "ledger_id": {"ledger_id": "other-ledger"},
            "requested_by_lane": {"requested_by_lane": "operator-lane"},
            "requesting_host": {"requesting_host": "operator-host"},
            "requested_at": {"requested_at": datetime(2026, 10, 2, tzinfo=UTC)},
            "principal": {"principal": "operator"},
        }
        request = ModelWorkLedgerAppendRequest.model_validate(
            request.model_dump() | changes[attack]
        )
    receipt = _handler(runner, LocalLedgerFile(path), "ledger").handle(request)
    assert receipt.status is EnumWorkLedgerAppendStatus.REFUSED
    assert receipt.exit_code == 65
    assert runner.calls == []
    assert path.read_bytes() == before
    assert receipt.principal is None


@pytest.mark.parametrize("row_type", ["RULING", "OPERATOR-CONSENT"])
def test_consent_only_from_operator_principal(tmp_path: Path, row_type: str) -> None:
    path = _ledger(tmp_path)
    runner = _Runner(path)
    operator_key = Ed25519PrivateKey.generate()
    developer_key = Ed25519PrivateKey.generate()
    handler = _handler(
        runner,
        LocalLedgerFile(path),
        "ledger",
        public_keys={
            "operator": operator_key.public_key(),
            "developer": developer_key.public_key(),
        },
        operator_principal="operator",
    )
    for principal, key in [("developer", developer_key), ("operator", operator_key)]:
        request_id = uuid4()
        request = _request(_row(request_id, row_type), request_id).signed(
            principal, key
        )
        receipt = handler.handle(request)
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
    ).signed("developer", _DEVELOPER_KEY)


def _ledger(tmp_path: Path) -> Path:
    path = tmp_path / "ledger.md"
    path.write_text("# Rolling ledger\n", encoding="utf-8")
    return path


def test_redelivered_request_appends_once(tmp_path: Path) -> None:
    path = _ledger(tmp_path)
    runner = _Runner(path)
    handler = _handler(runner, LocalLedgerFile(path), "ledger-host")
    request = _request()
    first = handler.handle(request)
    second = handler.handle(request)
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
    receipt = _handler(runner, LocalLedgerFile(path), "ledger").handle(_request())
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
        _request(rows, request_id)
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
        _request(rows, request_id)
    )
    assert receipt.status is EnumWorkLedgerAppendStatus.REFUSED
    assert receipt.exit_code == 65
    assert "row 2 (MSG)" in receipt.message
    assert runner.calls == []


@pytest.mark.parametrize("rows", ["not a row", "2026-10-01T12:00:00Z |NO| nope"])
def test_no_ledger_row_refused(tmp_path: Path, rows: str) -> None:
    path = _ledger(tmp_path)
    runner = _Runner(path)
    receipt = _handler(runner, LocalLedgerFile(path), "ledger").handle(_request(rows))
    assert receipt.status is EnumWorkLedgerAppendStatus.REFUSED
    assert receipt.exit_code == 65
    assert "no ledger row" in receipt.message
    assert runner.calls == []


def test_unknown_row_type_refused(tmp_path: Path) -> None:
    path = _ledger(tmp_path)
    runner = _Runner(path)
    request_id = uuid4()
    receipt = _handler(runner, LocalLedgerFile(path), "ledger").handle(
        _request(_row(request_id, "UNKNOWN"), request_id)
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
    first = handler.handle(_request(rows, request_id))
    second = handler.handle(_request(rows, request_id))
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
    receipt = handler.handle(_request())
    assert receipt.status.value == expected
    assert receipt.exit_code == codes[-1]
    assert len(runner.calls) == 3
    assert sleeps == [0, 0]


@pytest.mark.parametrize("code", [1, 70, 124])
def test_other_exit_is_error_with_bounded_tail(tmp_path: Path, code: int) -> None:
    path = _ledger(tmp_path)
    runner = _Runner(path, [code], "x" * 2100 + "final output")
    receipt = _handler(runner, LocalLedgerFile(path), "ledger").handle(_request())
    assert receipt.status is EnumWorkLedgerAppendStatus.ERROR
    assert receipt.exit_code == code
    assert receipt.message.endswith("final output")
    assert len(receipt.message) == 2000
    assert len(runner.calls) == 1


def test_exit_zero_with_dedup_stderr_is_accepted(tmp_path: Path) -> None:
    path = _ledger(tmp_path)
    runner = _Runner(path, [0], "DEDUP: already landed during append")
    receipt = _handler(runner, LocalLedgerFile(path), "ledger").handle(_request())
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
    ).signed("developer", _DEVELOPER_KEY)
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

    handler = _handler(Runner(), LocalLedgerFile(path), "ledger-host")
    receipt = handler.handle(
        ModelWorkLedgerAppendRequest(
            request_id=request_id,
            rows=row + "\n",
            requested_by_lane="a",
            requesting_host="h",
            requested_at=datetime.now(UTC),
        ).signed("developer", _DEVELOPER_KEY)
    )
    assert receipt.status is EnumWorkLedgerAppendStatus.ACCEPTED
    assert receipt.exit_code == 0
    assert "stranded clone" in receipt.message


@pytest.mark.parametrize("claimed", ["operator", "developer | principal=operator"])
def test_signed_rows_cannot_forge_or_duplicate_principal(
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
        _request(rows, request_id)
    )
    assert receipt.status is EnumWorkLedgerAppendStatus.REFUSED
    assert runner.calls == []
    assert path.read_bytes() == before


def test_verified_principal_stamped_once_on_every_row(tmp_path: Path) -> None:
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
    receipt = handler.handle(request)
    assert receipt.status is EnumWorkLedgerAppendStatus.ACCEPTED
    assert receipt.principal == "developer"
    assert runner.calls == [_attributed(rows)]
    assert runner.calls[0].count(" | principal=developer | ") == len(kinds)
    assert runner.calls[0].endswith("\r\n  continuation\r\n")
    assert handler.handle(request).status is EnumWorkLedgerAppendStatus.DUPLICATE


def test_matching_signed_principal_cell_is_preserved(tmp_path: Path) -> None:
    path = _ledger(tmp_path)
    runner = _Runner(path)
    request_id = uuid4()
    request = _request(_attributed(_row(request_id)), request_id)
    receipt = _handler(runner, LocalLedgerFile(path), "ledger").handle(request)
    assert receipt.status is EnumWorkLedgerAppendStatus.ACCEPTED
    assert runner.calls == [request.rows]


@pytest.mark.parametrize("attack", ["content", "continuation", "principal", "unsigned"])
def test_redelivery_cannot_bypass_authentication_or_change_rows(
    tmp_path: Path, attack: str
) -> None:
    path = _ledger(tmp_path)
    runner = _Runner(path)
    handler = _handler(runner, LocalLedgerFile(path), "ledger")
    original = _request(_row(request_id := uuid4()) + "\n  continuation", request_id)
    assert handler.handle(original).status is EnumWorkLedgerAppendStatus.ACCEPTED
    before = path.read_bytes()
    if attack == "unsigned":
        changed = original.model_copy(update={"signature": None})
    elif attack == "principal":
        changed = original.signed("operator", _OPERATOR_KEY)
    else:
        changed = original.model_copy(
            update={"rows": original.rows + " changed"}
        ).signed("developer", _DEVELOPER_KEY)
        if attack == "content":
            changed = original.model_copy(
                update={"rows": original.rows.replace("exact text", "changed")}
            ).signed("developer", _DEVELOPER_KEY)
    assert handler.handle(changed).status is EnumWorkLedgerAppendStatus.REFUSED
    assert len(runner.calls) == 1
    assert path.read_bytes() == before


def test_request_id_in_continuation_cannot_replace_header_cell(tmp_path: Path) -> None:
    path = _ledger(tmp_path)
    runner = _Runner(path)
    request_id = uuid4()
    rows = f"2026-10-01T12:00:00Z | STATUS | lane=test | text\n  continuation | req={request_id}"
    receipt = _handler(runner, LocalLedgerFile(path), "ledger").handle(
        _request(rows, request_id)
    )
    assert receipt.status is EnumWorkLedgerAppendStatus.REFUSED
    assert runner.calls == []


def test_no_issuer_records_refuses_even_valid_signature(tmp_path: Path) -> None:
    path = _ledger(tmp_path)
    runner = _Runner(path)
    receipt = HandlerWorkLedgerAppendEffect(
        runner, LocalLedgerFile(path), "ledger"
    ).handle(_request())
    assert receipt.status is EnumWorkLedgerAppendStatus.REFUSED
    assert runner.calls == []


def test_signed_row_cannot_hide_a_principal_behind_unspaced_pipe(
    tmp_path: Path,
) -> None:
    path = _ledger(tmp_path)
    runner = _Runner(path)
    request_id = uuid4()
    rows = _row(request_id).replace("lane=test", "lane=test|principal=operator")
    receipt = _handler(runner, LocalLedgerFile(path), "ledger").handle(
        _request(rows, request_id)
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
    assert handler.handle(request).status is EnumWorkLedgerAppendStatus.REFUSED
    assert runner.calls == []
    assert path.read_bytes() == before
    # Host and lane claims cannot substitute for the independently configured operator.
    operator = request.signed("operator", _OPERATOR_KEY)
    assert handler.handle(operator).status is EnumWorkLedgerAppendStatus.ACCEPTED
    assert runner.calls == [_attributed(rows, "operator")]
    assert handler.handle(operator).status is EnumWorkLedgerAppendStatus.DUPLICATE


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
        _request(_row(request_id) + "\n" + line, request_id)
    )
    assert receipt.status is EnumWorkLedgerAppendStatus.REFUSED
    assert runner.calls == []


@pytest.mark.parametrize(
    "principal",
    ["developer | principal=operator", "developer\noperator", " developer", ""],
)
def test_principal_name_cannot_inject_ledger_cells(principal: str) -> None:
    with pytest.raises(ValidationError):
        _request().signed(principal, _DEVELOPER_KEY)
