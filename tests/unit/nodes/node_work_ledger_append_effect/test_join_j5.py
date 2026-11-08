# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""J5 (piece 5 to piece 6): a ledger append under a store-resolved identity lands twice (OMN-22914).

Rebuild plan 4.1, J5: "A ledger append runs under a store-resolved identity and lands as a durable row and a
projected row". Every append here goes to an in-memory ledger or a scratch file in ``tmp_path``, never to the live
ledger file.

The join is judged the way the floor judges it: the piece 5 chain and the piece 6 chain share one correlation id and
the floor's own receipt fold reads the pair. The golden case reads ``golden_only`` and the error case ``error_only``;
the two together read ``passed``. The state the floor showed before this node answered piece 5 (a piece 5 failure and
a silent piece 6) is pinned by ``test_control_*`` with the reading it gave, ``broken``.

Two injected breaks show the tests can fail for real: an identity resolver that guesses a writer when the store has
none (the error case would then land a row) and a projection that never hears the row (the golden case would then
find no projected row).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
import yaml

from omnimarket.handlers.emission_sink import (
    ChainEmitter,
    InMemoryEmissionSink,
)
from omnimarket.models.floor_17a import (
    ModelFloorReceiptInput,
    ModelJoinDef,
    ModelNodeEmission,
    ModelPieceDef,
)
from omnimarket.models.work_ledger_append import (
    EnumJoinJ5Outcome,
    EnumWorkLedgerAppendStatus,
    ModelAppendIfAbsentPolicy,
    ModelJoinJ5Request,
    ModelWorkLedgerAppendRequest,
)
from omnimarket.nodes.node_floor_emission_compute.handlers.handler_floor_receipt import (
    HandlerFloorReceipt,
)
from omnimarket.nodes.node_work_ledger_append_effect.handlers.handler_work_ledger_append_effect import (
    HandlerWorkLedgerAppendEffect,
)
from omnimarket.nodes.node_work_ledger_append_effect.handlers.handler_work_ledger_join_j5 import (
    HandlerWorkLedgerJoinJ5,
    load_join_j5_policy,
)
from omnimarket.nodes.node_work_ledger_append_effect.protocols.join_j5 import (
    InMemoryJoinLedger,
    InMemoryProjectedRows,
    InMemoryStoreIdentities,
    ScratchFileJoinLedger,
    StoreIdentityUnresolvedError,
)

pytestmark = pytest.mark.unit

NOW = datetime(2026, 11, 7, 12, 0, 0, tzinfo=UTC)
IDENTITY_REF = "ledger-writer/lab-machine"
STORE_PRINCIPAL = "lab-machine"
LEDGER_ID = "rolling-work-ledger"
POLICY = load_join_j5_policy()
CHAIN_DEADLINE_S = 900


class _Clock:
    """A clock that moves one second per read, so a chain's events keep their order."""

    def __init__(self) -> None:
        self._t = NOW

    def __call__(self) -> datetime:
        self._t += timedelta(seconds=1)
        return self._t


def _row_text(request_id: object) -> str:
    return f"2026-11-07T12:00:00Z | STATUS | lane=j5-test | req={request_id} | j5 probe row"


def _request(
    cid: str, ref: str = IDENTITY_REF, rows: str | None = None
) -> ModelJoinJ5Request:
    request_id = uuid4()
    return ModelJoinJ5Request(
        correlation_id=cid,
        identity_ref=ref,
        append=ModelWorkLedgerAppendRequest(
            request_id=request_id,
            ledger_id=LEDGER_ID,
            rows=_row_text(request_id) if rows is None else rows.format(req=request_id),
            requested_by_lane="j5-test",
            requesting_host="test-host",
            requested_at=NOW,
        ),
    )


class _Rig:
    """The in-memory backend: durable text, a projection fed by it, a store, an emission sink."""

    def __init__(
        self,
        *,
        identities: dict[str, str] | None = None,
        projection_hears_ledger: bool = True,
        guess_writer_when_unresolved: bool = False,
        projection: InMemoryProjectedRows | None = None,
        header: str | None = None,
    ) -> None:
        self.projection = projection or InMemoryProjectedRows()
        self.heard = InMemoryProjectedRows()
        self.ledger = InMemoryJoinLedger(
            projection=self.projection if projection_hears_ledger else self.heard,
            **({} if header is None else {"header": header}),
        )
        self.store = InMemoryStoreIdentities(
            {IDENTITY_REF: STORE_PRINCIPAL} if identities is None else identities
        )
        self.sink = InMemoryEmissionSink()
        self.clock = _Clock()
        self.append = HandlerWorkLedgerAppendEffect(
            self.ledger,
            self.ledger,
            "test-host",
            writer_principals=(STORE_PRINCIPAL,),
            operator_principal="operator",
        )
        self.join = HandlerWorkLedgerJoinJ5(
            append=self.append,
            ledger=self.ledger,
            identities=(
                _GuessingIdentities(self.store)
                if guess_writer_when_unresolved
                else self.store
            ),
            projection=self.projection,
            sink=self.sink,
            policy=POLICY,
            clock=self.clock,
            backend="in_memory",
        )

    def upstream_chain(self, cid: str, *, resolves: bool) -> None:
        """Piece 5 as it answers the same store: a start, then a success or a typed failure."""
        chain = ChainEmitter(
            self.sink,
            node="node_secret_reading_effect",
            piece=POLICY.from_piece,
            clock=self.clock,
            correlation_id=cid,
            backend="in_memory",
        )
        chain.start()
        if resolves:
            chain.success({"keys_resolved": 1})
        else:
            chain.failure("identity_key_unresolved", "the store has no such key")


class _GuessingIdentities:
    """Injected break: answers with a default writer when the store has none."""

    def __init__(self, inner: InMemoryStoreIdentities) -> None:
        self._inner = inner

    def resolve(self, identity_ref: str) -> str:
        try:
            return self._inner.resolve(identity_ref)
        except StoreIdentityUnresolvedError:
            return STORE_PRINCIPAL


def _fold(
    emissions: list[ModelNodeEmission], now: datetime
) -> tuple[str, int | None, list[str]]:
    receipts = HandlerFloorReceipt().handle(
        ModelFloorReceiptInput(
            now=now + timedelta(seconds=5),
            emissions=tuple(emissions),
            pieces=tuple(
                ModelPieceDef(piece=p, name=f"piece {p}", reading_max_age_s=86400)
                for p in (POLICY.from_piece, POLICY.to_piece)
            ),
            joins=(
                ModelJoinDef(
                    id=POLICY.join_id,
                    from_piece=POLICY.from_piece,
                    to_piece=POLICY.to_piece,
                    label="secrets to ledger",
                ),
            ),
            freshness_max_age_s=86400,
            error_max_age_s=86400,
            chain_deadline_s=CHAIN_DEADLINE_S,
        )
    )
    join = next(j for j in receipts.joins if j.join == POLICY.join_id)
    return (
        join.state,
        (None if join.golden_at is None else 1),
        [f.code for f in receipts.findings],
    )


def _fold_state(emissions: list[ModelNodeEmission], now: datetime) -> str:
    return _fold(emissions, now)[0]


# --- the golden join ----------------------------------------------------------------------------------------------


def test_join_j5_golden_append_under_store_identity_lands_durable_and_projected() -> (
    None
):
    rig = _Rig()
    cid = uuid4().hex
    rig.upstream_chain(cid, resolves=True)
    request = _request(cid)

    result = rig.join.handle(request)

    assert result.outcome is EnumJoinJ5Outcome.LANDED
    assert result.failure_code is None
    # The receipt: accepted, attributed to the principal the store gave, with the ledger position.
    assert result.receipt is not None
    assert result.receipt.status is EnumWorkLedgerAppendStatus.ACCEPTED
    assert result.receipt.principal == STORE_PRINCIPAL
    assert result.ledger_seq == result.receipt.ledger_lines[0]
    # The durable row: on the ledger at that position, stamped with the store identity.
    durable = rig.ledger.read_text().splitlines()[result.ledger_seq - 1]
    assert f"principal={STORE_PRINCIPAL}" in durable
    assert f"req={request.append.request_id}" in durable
    # The projected row: the same row reads back from the projection at the same position.
    projected = rig.projection.read_row(
        ledger_id=LEDGER_ID, ledger_seq=result.ledger_seq
    )
    assert projected is not None
    assert projected.raw_row == durable
    assert projected.ledger_seq == result.ledger_seq
    assert result.projected_row_id == projected.row_id


def test_join_j5_golden_chain_event_piece_6_follows_piece_5_and_the_floor_reads_golden() -> (
    None
):
    rig = _Rig()
    cid = uuid4().hex
    rig.upstream_chain(cid, resolves=True)
    rig.join.handle(_request(cid))

    mine = [e for e in rig.sink.events if e.piece == POLICY.to_piece]
    assert [e.phase for e in mine] == ["start", "success"]
    assert {e.correlation_id for e in mine} == {cid}
    assert mine[-1].counters == {"rows_appended": 1, "rows_projected": 1}
    state, golden, findings = _fold(rig.sink.events, NOW)
    assert (state, findings) == ("golden_only", [])
    assert golden == 1


def test_join_j5_golden_on_a_scratch_file_ledger_never_the_live_one(
    tmp_path: Path,
) -> None:
    """The durable row is a real line in a scratch file; the projection hears it as the bus would."""
    scratch = tmp_path / "scratch-lane-ledger.md"
    projection = InMemoryProjectedRows()
    scratch_ledger = ScratchFileJoinLedger(scratch, projection=projection)
    sink = InMemoryEmissionSink()
    join = HandlerWorkLedgerJoinJ5(
        append=HandlerWorkLedgerAppendEffect(
            scratch_ledger,
            scratch_ledger,
            "test-host",
            writer_principals=(STORE_PRINCIPAL,),
            operator_principal="operator",
        ),
        ledger=scratch_ledger,
        identities=InMemoryStoreIdentities({IDENTITY_REF: STORE_PRINCIPAL}),
        projection=projection,
        sink=sink,
        policy=POLICY,
        clock=_Clock(),
        backend="in_memory",
    )

    result = join.handle(_request(uuid4().hex))

    assert result.outcome is EnumJoinJ5Outcome.LANDED
    on_disk = scratch.read_text(encoding="utf-8").splitlines()
    assert on_disk[result.ledger_seq - 1].endswith("j5 probe row")
    projected = projection.read_row(ledger_id=LEDGER_ID, ledger_seq=result.ledger_seq)
    assert projected is not None
    assert projected.raw_row == on_disk[result.ledger_seq - 1]


def test_join_j5_error_the_live_ledger_file_is_refused_as_a_scratch_lane(
    tmp_path: Path,
) -> None:
    """A scratch lane can never be the live ledger: its name is refused before anything is opened."""
    with pytest.raises(ValueError, match="live ledger"):
        ScratchFileJoinLedger(
            tmp_path / "ROLLING_WORK_LEDGER.md", projection=InMemoryProjectedRows()
        )
    assert not (tmp_path / "ROLLING_WORK_LEDGER.md").exists()


# --- the error join -----------------------------------------------------------------------------------------------


def test_join_j5_error_unresolved_identity_is_refused_typed_and_writes_no_row() -> None:
    rig = _Rig()
    cid = uuid4().hex
    rig.upstream_chain(cid, resolves=False)
    before = rig.ledger.read_text()

    result = rig.join.handle(_request(cid, ref="ledger-writer/not-in-store"))

    assert result.outcome is EnumJoinJ5Outcome.REFUSED_IDENTITY_UNRESOLVED
    assert result.failure_code == POLICY.identity_unresolved_failure_code
    assert result.failure_code.startswith("upstream_")
    assert result.receipt is None
    assert result.ledger_seq is None
    # No row in either place: the ledger text is byte for byte what it was, the append handler was never called, the
    # projection holds nothing.
    assert rig.ledger.read_text() == before
    assert rig.ledger.append_calls == []
    assert rig.projection.rows() == ()


def test_join_j5_error_chain_event_typed_failure_with_no_start_and_the_floor_reads_error() -> (
    None
):
    rig = _Rig()
    cid = uuid4().hex
    rig.upstream_chain(cid, resolves=False)
    rig.join.handle(_request(cid, ref="ledger-writer/not-in-store"))

    mine = [e for e in rig.sink.events if e.piece == POLICY.to_piece]
    assert [e.phase for e in mine] == ["failure"]
    assert mine[0].failure_code == POLICY.identity_unresolved_failure_code
    assert mine[0].correlation_id == cid
    state, _, findings = _fold(rig.sink.events, NOW)
    assert (state, findings) == ("error_only", [])


def test_join_j5_error_and_golden_together_read_passed_on_the_floor() -> None:
    rig = _Rig()
    good, bad = uuid4().hex, uuid4().hex
    rig.upstream_chain(good, resolves=True)
    rig.join.handle(_request(good))
    rig.upstream_chain(bad, resolves=False)
    rig.join.handle(_request(bad, ref="ledger-writer/not-in-store"))

    state, _, findings = _fold(rig.sink.events, NOW)
    assert (state, findings) == ("passed", [])


def test_join_j5_error_identity_the_ledger_does_not_know_is_refused_and_writes_no_row() -> (
    None
):
    """The store resolves a name, but it is no writer the ledger host serves: the append is refused, no row lands."""
    rig = _Rig(identities={IDENTITY_REF: "someone-else"})
    cid = uuid4().hex
    rig.upstream_chain(cid, resolves=True)
    before = rig.ledger.read_text()

    result = rig.join.handle(_request(cid))

    assert result.outcome is EnumJoinJ5Outcome.APPEND_NOT_ACCEPTED
    assert result.failure_code == POLICY.append_not_accepted_failure_code
    assert result.receipt is not None
    assert result.receipt.status is EnumWorkLedgerAppendStatus.REFUSED
    assert rig.ledger.read_text() == before
    assert rig.projection.rows() == ()
    mine = [e for e in rig.sink.events if e.piece == POLICY.to_piece]
    assert [e.phase for e in mine] == ["start", "failure"]


def test_join_j5_error_row_that_never_reaches_the_projection_fails_typed() -> None:
    rig = _Rig(projection_hears_ledger=False)
    cid = uuid4().hex
    rig.upstream_chain(cid, resolves=True)

    result = rig.join.handle(_request(cid))

    # The durable row landed and the join says the projected one did not: a typed failure, never a success.
    assert result.outcome is EnumJoinJ5Outcome.PROJECTED_ROW_MISSING
    assert result.failure_code == POLICY.projected_row_missing_failure_code
    assert result.ledger_seq is not None
    mine = [e for e in rig.sink.events if e.piece == POLICY.to_piece]
    assert [e.phase for e in mine] == ["start", "failure"]
    assert mine[-1].failure_code == POLICY.projected_row_missing_failure_code


# --- controls: the tests above can fail ---------------------------------------------------------------------------


def test_control_the_floor_state_before_this_node_piece_5_fails_and_piece_6_is_silent() -> (
    None
):
    """The reading the floor gave for J5: 22 piece 5 failures that piece 6 never answered read ``broken``."""
    sink = InMemoryEmissionSink()
    rig = _Rig()
    rig.sink = sink
    for _ in range(3):
        chain = ChainEmitter(
            sink,
            node="node_secret_reading_effect",
            piece=POLICY.from_piece,
            clock=rig.clock,
            correlation_id=uuid4().hex,
            backend="in_memory",
        )
        chain.start()
        chain.failure("identity_key_unresolved", "the store has no such key")

    later = NOW + timedelta(seconds=CHAIN_DEADLINE_S + 60)
    state, _, findings = _fold(sink.events, later)
    assert state == "broken"
    assert findings == ["downstream_missing_typed_failure"] * 3


def test_control_a_resolver_that_guesses_a_writer_lands_a_row_the_refusal_forbids() -> (
    None
):
    rig = _Rig(guess_writer_when_unresolved=True)
    cid = uuid4().hex
    rig.upstream_chain(cid, resolves=False)
    before = rig.ledger.read_text()

    result = rig.join.handle(_request(cid, ref="ledger-writer/not-in-store"))

    # With the break the unresolved identity is no longer refused: a row lands in both places, which is exactly what
    # the error test above asserts cannot happen.
    assert result.outcome is EnumJoinJ5Outcome.LANDED
    assert rig.ledger.read_text() != before
    assert rig.projection.rows() != ()
    # And the floor then names the break: piece 5 failed and piece 6 started.
    assert "downstream_started_after_upstream_failed" in _fold(rig.sink.events, NOW)[2]


def test_control_a_projection_that_never_hears_the_row_is_not_a_landed_join() -> None:
    rig = _Rig(projection_hears_ledger=False)
    cid = uuid4().hex
    rig.upstream_chain(cid, resolves=True)

    result = rig.join.handle(_request(cid))

    assert result.outcome is not EnumJoinJ5Outcome.LANDED
    assert rig.projection.read_row(ledger_id=LEDGER_ID, ledger_seq=2) is None
    # The durable half did land, which is why the join must read the projection and not trust the receipt.
    assert any("j5 probe row" in line for line in rig.ledger.read_text().splitlines())


def test_control_the_contract_declares_the_join_settings_and_the_unresolved_code_is_upstream() -> (
    None
):
    """Every setting of the join sits in the contract; the unresolved code keeps the floor's ``upstream_`` shape."""
    contract = yaml.safe_load(
        (
            Path(__file__).resolve().parents[4]
            / "src/omnimarket/nodes/node_work_ledger_append_effect/contract.yaml"
        ).read_text(encoding="utf-8")
    )
    section = contract["join_j5"]
    assert section["from_piece"] == POLICY.from_piece == 5
    assert section["to_piece"] == POLICY.to_piece == 6
    assert section["identity_unresolved_failure_code"].startswith("upstream_")
    with pytest.raises(ValueError, match="upstream_"):
        type(POLICY).model_validate(
            {**POLICY.model_dump(), "identity_unresolved_failure_code": "no_prefix"}
        )


# --- review findings (omnimarket#5286) ----------------------------------------------------------------------------

_TWO_ROWS = (
    "2026-11-07T12:00:00Z | STATUS | lane=j5-test | req={req} | first row\n"
    "2026-11-07T12:00:01Z | STATUS | lane=j5-test | req={req} | second row"
)


class _DropsSecondRow(InMemoryProjectedRows):
    """Injected break: the projection hears the first row of a request and never the second."""

    def project(self, *, raw_row: str, ledger_id: str, ledger_seq: int) -> None:
        if "second row" not in raw_row:
            super().project(raw_row=raw_row, ledger_id=ledger_id, ledger_seq=ledger_seq)


def test_join_j5_multi_row_append_is_not_landed_when_a_later_row_never_projects() -> (
    None
):
    rig = _Rig(projection=_DropsSecondRow())
    cid = uuid4().hex
    rig.upstream_chain(cid, resolves=True)

    result = rig.join.handle(_request(cid, rows=_TWO_ROWS))

    assert result.outcome is EnumJoinJ5Outcome.PROJECTED_ROW_MISSING
    mine = [e for e in rig.sink.events if e.piece == POLICY.to_piece]
    assert [e.phase for e in mine] == ["start", "failure"]


def test_join_j5_multi_row_append_counts_every_row_it_landed_and_projected() -> None:
    rig = _Rig()
    cid = uuid4().hex
    rig.upstream_chain(cid, resolves=True)

    result = rig.join.handle(_request(cid, rows=_TWO_ROWS))

    assert result.outcome is EnumJoinJ5Outcome.LANDED
    mine = [e for e in rig.sink.events if e.piece == POLICY.to_piece]
    assert mine[-1].counters == {"rows_appended": 2, "rows_projected": 2}


def test_join_j5_indented_continuation_is_part_of_the_projected_logical_row() -> None:
    rig = _Rig()
    cid = uuid4().hex
    rig.upstream_chain(cid, resolves=True)
    rows = (
        "2026-11-07T12:00:00Z | STATUS | lane=j5-test | req={req} | header text\n"
        "  continuation text kept with its row"
    )

    result = rig.join.handle(_request(cid, rows=rows))

    assert result.outcome is EnumJoinJ5Outcome.LANDED
    projected = rig.projection.read_row(
        ledger_id=LEDGER_ID, ledger_seq=result.ledger_seq
    )
    assert projected is not None
    assert projected.raw_row.endswith("\n  continuation text kept with its row")


def test_join_j5_append_after_a_ledger_without_a_final_newline_keeps_both_rows() -> (
    None
):
    rig = _Rig(header="# In-memory ledger")
    cid = uuid4().hex
    rig.upstream_chain(cid, resolves=True)

    result = rig.join.handle(_request(cid))

    assert result.outcome is EnumJoinJ5Outcome.LANDED
    assert rig.ledger.read_text().splitlines()[0] == "# In-memory ledger"
    assert result.ledger_seq == 2


def test_join_j5_scratch_file_without_a_final_newline_keeps_its_last_line(
    tmp_path: Path,
) -> None:
    scratch = tmp_path / "scratch-lane-ledger.md"
    scratch.write_text("# Scratch lane ledger", encoding="utf-8")
    projection = InMemoryProjectedRows()
    scratch_ledger = ScratchFileJoinLedger(scratch, projection=projection)
    row = "2026-11-07T12:00:00Z | STATUS | lane=j5-test | req=x | row"

    scratch_ledger.append(row)

    assert scratch.read_text(encoding="utf-8").splitlines() == [
        "# Scratch lane ledger",
        row,
    ]
    assert projection.read_row(ledger_id=LEDGER_ID, ledger_seq=2) is not None


def test_join_j5_replayed_request_keeps_one_valid_floor_chain() -> None:
    rig = _Rig()
    cid = uuid4().hex
    rig.upstream_chain(cid, resolves=True)
    request = _request(cid)

    first = rig.join.handle(request)
    again = rig.join.handle(request)

    assert first.outcome is EnumJoinJ5Outcome.LANDED
    assert again.outcome is EnumJoinJ5Outcome.LANDED
    assert again.receipt is not None
    assert again.receipt.status is EnumWorkLedgerAppendStatus.DUPLICATE
    mine = [e for e in rig.sink.events if e.piece == POLICY.to_piece]
    assert [e.phase for e in mine] == ["start", "success"]
    state, _, findings = _fold(rig.sink.events, NOW)
    assert (state, findings) == ("golden_only", [])


def test_join_j5_retry_after_a_projection_failure_does_not_add_a_success_to_the_failed_chain() -> (
    None
):
    rig = _Rig(projection_hears_ledger=False)
    cid = uuid4().hex
    rig.upstream_chain(cid, resolves=True)
    request = _request(cid)
    assert rig.join.handle(request).outcome is EnumJoinJ5Outcome.PROJECTED_ROW_MISSING
    rig.heard.project(
        raw_row=rig.ledger.read_text().splitlines()[1],
        ledger_id=LEDGER_ID,
        ledger_seq=2,
    )
    rig.projection.project(
        raw_row=rig.ledger.read_text().splitlines()[1],
        ledger_id=LEDGER_ID,
        ledger_seq=2,
    )

    rig.join.handle(request)

    _, _, findings = _fold(rig.sink.events, NOW)
    assert findings == []


def test_join_j5_conditional_no_op_on_another_writers_row_is_not_landed() -> None:
    """ALREADY_PRESENT names a row the request did not write; it is no evidence the requested row landed."""
    rig = _Rig()
    cid = uuid4().hex
    rig.upstream_chain(cid, resolves=True)
    other = "2026-11-07T11:00:00Z | STATUS | lane=other | principal=someone-else | req=other | id=ROW1 | their row"
    rig.ledger.append(other)
    append = HandlerWorkLedgerAppendEffect(
        rig.ledger,
        rig.ledger,
        "test-host",
        writer_principals=(STORE_PRINCIPAL,),
        operator_principal="operator",
        append_if_absent=ModelAppendIfAbsentPolicy(
            row_id_cell="id", lock_suffix=".lock", lock_wait_seconds=1.0
        ),
    )
    rig.join._append = append
    request = _request(
        cid,
        rows="2026-11-07T12:00:00Z | STATUS | lane=j5-test | req={req} | id=ROW1 | mine",
    )
    request = request.model_copy(
        update={
            "append": request.append.model_copy(
                update={"append_if_absent_row_id": "ROW1"}
            )
        }
    )

    result = rig.join.handle(request)

    assert result.receipt is not None
    assert result.receipt.status is EnumWorkLedgerAppendStatus.ALREADY_PRESENT
    assert result.outcome is not EnumJoinJ5Outcome.LANDED
    mine = [e for e in rig.sink.events if e.piece == POLICY.to_piece]
    assert [e.phase for e in mine] == ["start", "failure"]


def test_join_j5_scratch_symlink_to_the_live_ledger_name_is_refused(
    tmp_path: Path,
) -> None:
    live = tmp_path / "ROLLING_WORK_LEDGER.md"
    live.write_text("# live\n", encoding="utf-8")
    alias = tmp_path / "scratch-alias.md"
    alias.symlink_to(live)

    with pytest.raises(ValueError, match="live ledger"):
        ScratchFileJoinLedger(alias, projection=InMemoryProjectedRows())
    assert live.read_text(encoding="utf-8") == "# live\n"


def test_join_j5_row_ending_in_a_space_is_landed_not_reported_missing() -> None:
    rig = _Rig()
    cid = uuid4().hex
    rig.upstream_chain(cid, resolves=True)
    rows = "2026-11-07T12:00:00Z | STATUS | lane=j5-test | req={req} | trailing space "

    result = rig.join.handle(_request(cid, rows=rows))

    assert result.outcome is EnumJoinJ5Outcome.LANDED
