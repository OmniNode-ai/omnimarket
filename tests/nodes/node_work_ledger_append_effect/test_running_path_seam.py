# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Seam: the grammar's running-path rule and the work-ledger append effect (OMN-24535).

The grammar (``ledger_row_grammar.running_path_refusal``) decides; the writer
(``HandlerWorkLedgerAppendEffect``) refuses with the grammar's own text in file mode and in bus
mode, before the lock and before anything is appended. The test fails when either side changes
shape: the grammar's wording changes the writer's receipt (case 4), and the writer's wiring is caught
by the refusal of the real row (case 1). The failing control is a real ledger row of 2026-11-14: a
lane closed ``outcome=done`` with no reading of the path that runs the change.

The in-memory store stands for the bus here; the same refusal on the real broker is the
running reading the ticket's last criterion owns (the ledger serve on the dev bus), not a pytest.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import pytest

from omnimarket.handlers import ledger_row_grammar as grammar
from omnimarket.handlers import ledger_running_path_cell as running_path_cell
from omnimarket.models.work_ledger_append import EnumLedgerAppendMode
from omnimarket.nodes.node_work_ledger_append_effect import (
    EnumWorkLedgerAppendStatus,
    HandlerWorkLedgerAppendEffect,
    ModelWorkLedgerAppendReceipt,
    ModelWorkLedgerAppendRequest,
)
from omnimarket.nodes.node_work_ledger_append_effect.adapters import (
    InMemoryBusLedgerStore,
)
from omnimarket.nodes.node_work_ledger_append_effect.handlers import LedgerFlipFence
from omnimarket.nodes.node_work_ledger_append_effect.protocols import (
    LocalLedgerFile,
    ModelAppendCommandResult,
)

pytestmark = pytest.mark.unit

PRINCIPAL = "dev-lane-principal"
CUTOVER = "2026-11-14T00:00:00Z"
REQUEST_ID = UUID("5eb94e1d-3ffd-4ff2-8ae4-4f6d4af891f1")
# The real row, verbatim except for the principal name (renamed) and the trailing via= cell, which the writer stamps itself.
REAL_ROW_NO_READING = (
    "2026-11-14T19:33:58Z | TERMINAL | lane=fill-ticket-OMN-23215-20261114T1908Z | "
    f"principal={PRINCIPAL} | ticket=OMN-23215 | "
    "actor=claude:sonnet:remote-lane | model=claude-sonnet | friction=none | "
    "closes-CLAIM=2026-11-14T19:32:27Z | outcome=done | host=h053 | "
    "run=rlane-fill-ticket-OMN-23215-20261114T1908Z-9ec20d | "
    "delegated=0 delegation_reason=no-text-or-code | "
    f"req={REQUEST_ID} | remote lane on h053 "
    "ended status=done engine_rc=0; receipt "
    "rlane-fill-ticket-OMN-23215-20261114T1908Z-9ec20d.json on the launching host; "
    "host closeout: the lane made no worktrees"
)
CORRECTED_TWIN = REAL_ROW_NO_READING.replace(
    " | delegated=0",
    " | running_path=none-ci-only:fill-lane-opens-no-pull-request | delegated=0",
)
MACHINE_ROW = (
    "2026-11-14T19:33:30Z | TERMINAL | lane=landing-cause-L38756 | "
    f"principal={PRINCIPAL} | ticket=OMN-20413 | actor=script:landing-controller | "
    "model=none | friction=none | outcome=handed-off | "
    f"req={REQUEST_ID} | cause lease released; the controller reads its result file"
)


class _FileRunner:
    """The file judge's append: writes the rows it is handed to the ledger file."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.calls = 0

    def append(self, rows: str) -> ModelAppendCommandResult:
        self.calls += 1
        with self.path.open("a", encoding="utf-8") as stream:
            stream.write(rows + "\n")
        return ModelAppendCommandResult(exit_code=0)


class _Seam:
    """One writer in one mode, and how to read what landed in it."""

    def __init__(self, mode: str, tmp_path: Path) -> None:
        self.path = tmp_path / "ledger.md"
        self.path.write_text("", encoding="utf-8")
        self.runner = _FileRunner(self.path)
        self.store = InMemoryBusLedgerStore()
        self.handler = HandlerWorkLedgerAppendEffect(
            self.runner,
            LocalLedgerFile(self.path),
            "ledger",
            writer_principals=(PRINCIPAL,),
            operator_principal="operator",
            fence=LedgerFlipFence(),
            bus_store=self.store,
        )
        if mode == "bus":
            assert self.handler.flip_to_bus(drain_timeout_s=5).flipped is True
            assert self.handler.mode is EnumLedgerAppendMode.BUS

    def send(self, row: str) -> ModelWorkLedgerAppendReceipt:
        return self.handler.handle(
            ModelWorkLedgerAppendRequest(
                request_id=REQUEST_ID,
                rows=row,
                requested_by_lane="root1-runread-6805",
                requesting_host="lab-host",
                requested_at=datetime.now(UTC),
            ),
            writer_principal=PRINCIPAL,
        )

    def landed(self) -> str:
        return self.path.read_text(encoding="utf-8") + self.store.text()


@pytest.fixture(params=["file", "bus"])
def seam(request: pytest.FixtureRequest, tmp_path: Path) -> _Seam:
    return _Seam(request.param, tmp_path)


@pytest.fixture
def enforced(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(running_path_cell.ENFORCE_FROM_ENV, CUTOVER)


@pytest.fixture(autouse=True)
def _off_unless_set(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(running_path_cell.ENFORCE_FROM_ENV, raising=False)


@pytest.mark.usefixtures("enforced")
def test_error_chain_the_real_done_row_with_no_reading_is_refused_and_nothing_lands(
    seam: _Seam,
) -> None:
    receipt = seam.send(REAL_ROW_NO_READING)
    assert receipt.status is EnumWorkLedgerAppendStatus.REFUSED
    assert receipt.exit_code == 65
    assert "OMN-24535" in receipt.message
    assert "running-path cell" in receipt.message
    assert seam.landed() == ""
    assert seam.runner.calls == 0


@pytest.mark.usefixtures("enforced")
def test_golden_the_corrected_twin_is_accepted_and_lands(seam: _Seam) -> None:
    receipt = seam.send(CORRECTED_TWIN)
    assert receipt.status is EnumWorkLedgerAppendStatus.ACCEPTED
    assert "running_path=none-ci-only:fill-lane-opens-no-pull-request" in seam.landed()


def test_the_switch_is_what_the_control_flips_off_by_default_the_real_row_lands(
    seam: _Seam,
) -> None:
    receipt = seam.send(REAL_ROW_NO_READING)
    assert receipt.status is EnumWorkLedgerAppendStatus.ACCEPTED
    assert "outcome=done" in seam.landed()


@pytest.mark.usefixtures("enforced")
def test_seam_the_writer_refuses_with_the_grammars_own_text(seam: _Seam) -> None:
    """A change to the grammar's wording or shape changes the writer's receipt: they are one source."""
    expected = grammar.running_path_refusal(REAL_ROW_NO_READING, CUTOVER)
    assert expected is not None
    assert seam.send(REAL_ROW_NO_READING).message == f"row 1: {expected}"


@pytest.mark.usefixtures("enforced")
def test_a_machine_identity_is_not_judged(seam: _Seam) -> None:
    receipt = seam.send(MACHINE_ROW)
    assert receipt.status is EnumWorkLedgerAppendStatus.ACCEPTED


@pytest.mark.usefixtures("enforced")
def test_one_bad_row_in_a_payload_refuses_the_whole_payload_and_names_its_number(
    seam: _Seam,
) -> None:
    status_row = f"2026-11-14T19:33:00Z | STATUS | lane=lane-a | ticket=OMN-1 | req={REQUEST_ID} | text"
    receipt = seam.send(f"{status_row}\n{REAL_ROW_NO_READING}")
    assert receipt.status is EnumWorkLedgerAppendStatus.REFUSED
    assert receipt.message.startswith("row 2: ")
    assert seam.landed() == ""
