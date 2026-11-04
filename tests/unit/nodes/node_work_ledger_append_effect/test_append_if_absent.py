# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""append_if_absent: one row per row id, even under concurrent callers (OMN-22185).

The falsifier of acceptance criterion 3 is the concurrency test with its positive
controls: the same race against the plain append operation, and against the
same operation with the lock removed, does leave two rows.
"""

import builtins
import importlib
import multiprocessing
import socket
import subprocess
import threading
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from datetime import UTC, datetime
from functools import partial
from importlib import resources
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
import yaml
from pydantic import ValidationError

from omnimarket.models.work_ledger_append import ModelAppendIfAbsentPolicy
from omnimarket.nodes.node_work_ledger_append_effect import (
    EnumWorkLedgerAppendStatus,
    HandlerWorkLedgerAppendEffect,
    ModelWorkLedgerAppendRequest,
)
from omnimarket.nodes.node_work_ledger_append_effect.local_serve import (
    load_append_if_absent_policy,
    local_serve_handler,
)
from omnimarket.nodes.node_work_ledger_append_effect.protocols import (
    InMemoryLedgerAppendLock,
    LocalLedgerAppendLock,
    LocalLedgerFile,
    ModelAppendCommandResult,
)

pytestmark = pytest.mark.unit

POLICY = ModelAppendIfAbsentPolicy(
    row_id_cell="id", lock_suffix=".test-append.lock", lock_wait_seconds=30
)


class _FileRunner:
    """Appends to a real file, slowly enough that an unlocked race shows."""

    def __init__(self, path: Path, delay_s: float = 0.05) -> None:
        self._path = path
        self._delay_s = delay_s
        self.calls = 0

    def append(self, rows: str) -> ModelAppendCommandResult:
        self.calls += 1
        threading.Event().wait(self._delay_s)
        with self._path.open("a", encoding="utf-8") as stream:
            stream.write(rows + "\n")
        return ModelAppendCommandResult(exit_code=0)


class _TextReader:
    def __init__(self, text: str = "# Rolling ledger\n") -> None:
        self.text = text

    def read_text(self) -> str:
        return self.text


class _MemoryRunner:
    def __init__(self, reader: _TextReader) -> None:
        self._reader = reader
        self.calls: list[str] = []

    def append(self, rows: str) -> ModelAppendCommandResult:
        self.calls.append(rows)
        self._reader.text += rows + "\n"
        return ModelAppendCommandResult(exit_code=0)


class _BarrierReader:
    """Holds each reader until two readers have read: both checks happen before either append."""

    def __init__(self, path: Path, barrier: threading.Barrier) -> None:
        self._file = LocalLedgerFile(path)
        self._barrier = barrier
        self._seen = threading.local()

    def read_text(self) -> str:
        text = self._file.read_text()
        if not getattr(self._seen, "waited", False):
            self._seen.waited = True
            self._barrier.wait(timeout=10)
        return text


def _ledger(tmp_path: Path) -> Path:
    path = tmp_path / "ledger.md"
    path.write_text("# Rolling ledger\n", encoding="utf-8")
    return path


def _row(request_id: UUID, row_id: str) -> str:
    return (
        f"2026-11-03T12:00:00Z | STATUS | lane=test | id={row_id} | req={request_id}"
        " | exact text"
    )


def _request(
    row_id: str | None, *, plain_id: str | None = None, request_id: UUID | None = None
) -> ModelWorkLedgerAppendRequest:
    request_id = request_id or uuid4()
    cell_id = row_id if row_id is not None else plain_id
    assert cell_id is not None
    return ModelWorkLedgerAppendRequest(
        request_id=request_id,
        rows=_row(request_id, cell_id),
        requested_by_lane="lab",
        requesting_host="lab-host",
        requested_at=datetime.now(UTC),
        append_if_absent_row_id=row_id,
    )


def _handler(
    runner: Any, reader: Any, lock: Any, policy: ModelAppendIfAbsentPolicy | None
) -> HandlerWorkLedgerAppendEffect:
    return HandlerWorkLedgerAppendEffect(
        runner,
        reader,
        "ledger-host",
        writer_principals=("developer",),
        operator_principal="operator",
        append_lock=lock,
        append_if_absent=policy,
    )


def _file_handler(path: Path) -> HandlerWorkLedgerAppendEffect:
    """The serve process's own wiring: contract policy, flock beside the ledger."""
    return local_serve_handler(
        _FileRunner(path), path, "ledger-host", ("developer",), "operator"
    )


def _rows_with_id(path: Path, row_id: str) -> list[str]:
    return [
        line
        for line in path.read_text(encoding="utf-8").splitlines()
        if f" | id={row_id} | " in line
    ]


def _append_once(path: Path, row_id: str) -> Any:
    return _file_handler(path).handle(_request(row_id), writer_principal="developer")


def _race(calls: list[Any]) -> list[Any]:
    """Run every call at once from separate threads; return their results."""
    start = threading.Barrier(len(calls))

    def run(call: Any) -> Any:
        start.wait(timeout=10)
        return call()

    with ThreadPoolExecutor(max_workers=len(calls)) as pool:
        return [f.result() for f in [pool.submit(run, c) for c in calls]]


# (a) one call appends one row and returns its line.
def test_append_if_absent_appends_one_row_and_returns_its_line(tmp_path: Path) -> None:
    path = _ledger(tmp_path)
    receipt = _file_handler(path).handle(
        _request("row-1"), writer_principal="developer"
    )
    assert receipt.status is EnumWorkLedgerAppendStatus.ACCEPTED
    assert receipt.exit_code == 0
    assert receipt.ledger_lines == [2]
    assert len(_rows_with_id(path, "row-1")) == 1


# (b) a second call with the same row id appends nothing and returns the existing line.
def test_second_call_of_one_row_id_is_already_present(tmp_path: Path) -> None:
    path = _ledger(tmp_path)
    handler = _file_handler(path)
    first = handler.handle(_request("row-1"), writer_principal="developer")
    before = path.read_bytes()
    second = handler.handle(_request("row-1"), writer_principal="developer")
    assert first.status is EnumWorkLedgerAppendStatus.ACCEPTED
    assert second.status is EnumWorkLedgerAppendStatus.ALREADY_PRESENT
    assert second.status.value == "already-present"
    assert second.exit_code == 0
    assert second.ledger_lines == first.ledger_lines == [2]
    assert path.read_bytes() == before


def test_redelivery_of_an_accepted_request_is_still_a_duplicate(tmp_path: Path) -> None:
    path = _ledger(tmp_path)
    handler = _file_handler(path)
    request = _request("row-1")
    handler.handle(request, writer_principal="developer")
    again = handler.handle(request, writer_principal="developer")
    assert again.status is EnumWorkLedgerAppendStatus.DUPLICATE


# (c) two concurrent calls of one row id leave exactly one row.
def test_two_concurrent_calls_of_one_row_id_leave_one_row(tmp_path: Path) -> None:
    path = _ledger(tmp_path)
    receipts = _race(
        [
            lambda: _file_handler(path).handle(
                _request("row-1"), writer_principal="developer"
            )
            for _ in range(2)
        ]
    )
    assert sorted(r.status.value for r in receipts) == ["accepted", "already-present"]
    assert len(_rows_with_id(path, "row-1")) == 1
    assert {tuple(r.ledger_lines) for r in receipts} == {(2,)}


def test_many_concurrent_calls_of_one_row_id_leave_one_row(tmp_path: Path) -> None:
    path = _ledger(tmp_path)
    receipts = _race(
        [
            lambda: _file_handler(path).handle(
                _request("row-1"), writer_principal="developer"
            )
            for _ in range(8)
        ]
    )
    statuses = [r.status for r in receipts]
    assert statuses.count(EnumWorkLedgerAppendStatus.ACCEPTED) == 1
    assert statuses.count(EnumWorkLedgerAppendStatus.ALREADY_PRESENT) == 7
    assert len(_rows_with_id(path, "row-1")) == 1


def _process_call(path: str, row_id: str) -> str:
    ledger = Path(path)
    receipt = _file_handler(ledger).handle(
        _request(row_id), writer_principal="developer"
    )
    return receipt.status.value


def test_two_concurrent_processes_of_one_row_id_leave_one_row(tmp_path: Path) -> None:
    path = _ledger(tmp_path)
    with ProcessPoolExecutor(
        max_workers=2, mp_context=multiprocessing.get_context("spawn")
    ) as pool:
        futures = [pool.submit(_process_call, str(path), "row-1") for _ in range(2)]
        statuses = sorted(f.result(timeout=120) for f in futures)
    assert statuses == ["accepted", "already-present"]
    assert len(_rows_with_id(path, "row-1")) == 1


# (d) positive controls: the same race can leave two rows, so the test above can fail.
def test_control_plain_append_of_one_row_id_races_to_two_rows(tmp_path: Path) -> None:
    path = _ledger(tmp_path)
    receipts = _race(
        [
            lambda: _file_handler(path).handle(
                _request(None, plain_id="row-1"), writer_principal="developer"
            )
            for _ in range(2)
        ]
    )
    assert [r.status for r in receipts] == [EnumWorkLedgerAppendStatus.ACCEPTED] * 2
    assert len(_rows_with_id(path, "row-1")) == 2


def test_control_without_the_lock_the_check_and_append_race_to_two_rows(
    tmp_path: Path,
) -> None:
    """The lock is what makes the operation atomic: remove it and two rows land."""
    path = _ledger(tmp_path)
    barrier = threading.Barrier(2)

    class _NoLock:
        def hold(self) -> Any:
            return _NullContext()

    class _NullContext:
        def __enter__(self) -> None:
            return None

        def __exit__(self, *exc: object) -> None:
            return None

    def call() -> Any:
        handler = _handler(
            _FileRunner(path), _BarrierReader(path, barrier), _NoLock(), POLICY
        )
        return handler.handle(_request("row-1"), writer_principal="developer")

    receipts = _race([call, call])
    assert [r.status for r in receipts] == [EnumWorkLedgerAppendStatus.ACCEPTED] * 2
    assert len(_rows_with_id(path, "row-1")) == 2


def test_two_different_row_ids_leave_two_rows(tmp_path: Path) -> None:
    path = _ledger(tmp_path)
    receipts = _race(
        [partial(_append_once, path, row_id) for row_id in ("row-1", "row-2")]
    )
    assert [r.status for r in receipts] == [EnumWorkLedgerAppendStatus.ACCEPTED] * 2
    assert len(_rows_with_id(path, "row-1")) == 1
    assert len(_rows_with_id(path, "row-2")) == 1


# Refusals: the operation refuses what it cannot make atomic.
def test_row_without_the_id_cell_is_refused(tmp_path: Path) -> None:
    path = _ledger(tmp_path)
    request_id = uuid4()
    request = ModelWorkLedgerAppendRequest(
        request_id=request_id,
        rows=f"2026-11-03T12:00:00Z | STATUS | lane=test | id=other | req={request_id} | t",
        requested_by_lane="lab",
        requesting_host="lab-host",
        requested_at=datetime.now(UTC),
        append_if_absent_row_id="row-1",
    )
    before = path.read_bytes()
    receipt = _file_handler(path).handle(request, writer_principal="developer")
    assert receipt.status is EnumWorkLedgerAppendStatus.REFUSED
    assert receipt.exit_code == 65
    assert "id=row-1" in receipt.message
    assert path.read_bytes() == before


def test_more_than_one_row_is_refused(tmp_path: Path) -> None:
    path = _ledger(tmp_path)
    request_id = uuid4()
    rows = _row(request_id, "row-1") + "\n" + _row(request_id, "row-2")
    request = ModelWorkLedgerAppendRequest(
        request_id=request_id,
        rows=rows,
        requested_by_lane="lab",
        requesting_host="lab-host",
        requested_at=datetime.now(UTC),
        append_if_absent_row_id="row-1",
    )
    receipt = _file_handler(path).handle(request, writer_principal="developer")
    assert receipt.status is EnumWorkLedgerAppendStatus.REFUSED
    assert "exactly one row" in receipt.message


def test_unconfigured_policy_refuses_naming_the_missing_policy(tmp_path: Path) -> None:
    path = _ledger(tmp_path)
    handler = _handler(
        _FileRunner(path), LocalLedgerFile(path), InMemoryLedgerAppendLock(), None
    )
    receipt = handler.handle(_request("row-1"), writer_principal="developer")
    assert receipt.status is EnumWorkLedgerAppendStatus.REFUSED
    assert "append_if_absent" in receipt.message
    assert len(_rows_with_id(path, "row-1")) == 0


@pytest.mark.parametrize("bad", ["", "a|b", "a b", "a\nb", "x" * 201])
def test_row_id_must_be_one_pipe_free_token(bad: str) -> None:
    with pytest.raises(ValidationError):
        _request(bad)


# Lock: a held lock makes the second caller wait, then give up with the lock's exit code.
def test_lock_wait_timeout_is_an_error_with_exit_75(tmp_path: Path) -> None:
    path = _ledger(tmp_path)
    lock_path = path.with_name("held.lock")
    holder = LocalLedgerAppendLock(lock_path, 5)
    handler = _handler(
        _FileRunner(path),
        LocalLedgerFile(path),
        LocalLedgerAppendLock(lock_path, 0.2),
        POLICY,
    )
    with holder.hold():
        receipt = handler.handle(_request("row-1"), writer_principal="developer")
    assert receipt.status is EnumWorkLedgerAppendStatus.ERROR
    assert receipt.exit_code == 75
    assert len(_rows_with_id(path, "row-1")) == 0


# NP1: the node boots from its contract with in-memory adapters; no file, process or socket outside the seam.
def test_np1_node_boots_from_contract_with_in_memory_adapters(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    contract = yaml.safe_load(
        resources.files("omnimarket.nodes.node_work_ledger_append_effect")
        .joinpath("contract.yaml")
        .read_text()
    )
    declared = contract["handler"]
    handler_class = getattr(
        importlib.import_module(declared["module"]), declared["class"]
    )
    reader = _TextReader()
    runner = _MemoryRunner(reader)

    def refuse(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("handler reached outside the protocol seam")

    monkeypatch.setattr(builtins, "open", refuse)
    monkeypatch.setattr(subprocess, "Popen", refuse)
    monkeypatch.setattr(subprocess, "run", refuse)
    monkeypatch.setattr(socket, "socket", refuse)
    handler = handler_class(
        runner,
        reader,
        "ledger-host",
        writer_principals=("developer",),
        append_lock=InMemoryLedgerAppendLock(),
        append_if_absent=POLICY,
    )
    first = handler.handle(_request("row-1"), writer_principal="developer")
    second = handler.handle(_request("row-1"), writer_principal="developer")
    assert first.status is EnumWorkLedgerAppendStatus.ACCEPTED
    assert second.status is EnumWorkLedgerAppendStatus.ALREADY_PRESENT
    assert len(runner.calls) == 1


def test_np1_default_lock_is_in_memory_so_a_bare_handler_needs_no_file() -> None:
    reader = _TextReader()
    handler = HandlerWorkLedgerAppendEffect(
        _MemoryRunner(reader),
        reader,
        "ledger-host",
        writer_principals=("developer",),
        append_if_absent=POLICY,
    )
    receipt = handler.handle(_request("row-1"), writer_principal="developer")
    assert receipt.status is EnumWorkLedgerAppendStatus.ACCEPTED


# NP2: every name, path suffix and number is a contract field; a changed value changes the output.
def test_np2_policy_comes_from_the_contract() -> None:
    contract = yaml.safe_load(
        resources.files("omnimarket.nodes.node_work_ledger_append_effect")
        .joinpath("contract.yaml")
        .read_text()
    )
    declared = contract["append_if_absent"]
    policy = load_append_if_absent_policy()
    assert policy.row_id_cell == declared["row_id_cell"]
    assert policy.lock_suffix == declared["lock_suffix"]
    assert policy.lock_wait_seconds == declared["lock_wait_seconds"]


def _contract() -> dict[str, Any]:
    contract = yaml.safe_load(
        resources.files("omnimarket.nodes.node_work_ledger_append_effect")
        .joinpath("contract.yaml")
        .read_text()
    )
    assert isinstance(contract, dict)
    return contract


@pytest.mark.parametrize("key", ["row_id_cell", "lock_suffix", "lock_wait_seconds"])
def test_np2_absent_key_refuses_naming_it(key: str) -> None:
    contract = _contract()
    del contract["append_if_absent"][key]
    with pytest.raises(ValueError, match=key):
        load_append_if_absent_policy(contract)


def test_np2_absent_section_refuses_naming_it() -> None:
    contract = _contract()
    del contract["append_if_absent"]
    with pytest.raises(ValueError, match="append_if_absent"):
        load_append_if_absent_policy(contract)


def test_np2_changed_lock_suffix_changes_the_lock_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    contract = _contract()
    contract["append_if_absent"]["lock_suffix"] = ".changed.lock"
    monkeypatch.setattr(
        "omnimarket.nodes.node_work_ledger_append_effect.local_serve.yaml.safe_load",
        lambda _text: contract,
    )
    path = _ledger(tmp_path)
    _file_handler(path).handle(_request("row-1"), writer_principal="developer")
    assert sorted(p.name for p in tmp_path.iterdir()) == [
        "ledger.md",
        "ledger.md.changed.lock",
    ]


def test_serve_wiring_locks_beside_the_ledger_with_the_contract_suffix(
    tmp_path: Path,
) -> None:
    path = _ledger(tmp_path)
    _file_handler(path).handle(_request("row-1"), writer_principal="developer")
    suffix = _contract()["append_if_absent"]["lock_suffix"]
    assert (tmp_path / f"ledger.md{suffix}").exists()


def test_np2_changed_row_id_cell_changes_the_match(tmp_path: Path) -> None:
    """With the cell named `key`, an `id=` row does not count as present; a `key=` row does."""
    path = _ledger(tmp_path)
    keyed = ModelAppendIfAbsentPolicy(
        row_id_cell="key", lock_suffix=".k.lock", lock_wait_seconds=5
    )
    request_id = uuid4()
    request = ModelWorkLedgerAppendRequest(
        request_id=request_id,
        rows=f"2026-11-03T12:00:00Z | STATUS | lane=test | key=row-1 | req={request_id} | t",
        requested_by_lane="lab",
        requesting_host="lab-host",
        requested_at=datetime.now(UTC),
        append_if_absent_row_id="row-1",
    )
    handler = _handler(
        _FileRunner(path), LocalLedgerFile(path), InMemoryLedgerAppendLock(), keyed
    )
    assert (
        handler.handle(request, writer_principal="developer").status
        is EnumWorkLedgerAppendStatus.ACCEPTED
    )
    # The `id=`-named policy refuses the same row: it carries no id=row-1 cell.
    refused = _handler(
        _FileRunner(path), LocalLedgerFile(path), InMemoryLedgerAppendLock(), POLICY
    ).handle(request, writer_principal="developer")
    assert refused.status is EnumWorkLedgerAppendStatus.REFUSED


@pytest.mark.parametrize(
    "bad",
    [
        {"row_id_cell": "a|b"},
        {"row_id_cell": ""},
        {"lock_wait_seconds": 0},
        {"lock_suffix": "/etc"},
    ],
)
def test_np2_policy_model_validates(bad: dict[str, Any]) -> None:
    base = {"row_id_cell": "id", "lock_suffix": ".l.lock", "lock_wait_seconds": 1}
    with pytest.raises(ValidationError):
        ModelAppendIfAbsentPolicy(**{**base, **bad})
