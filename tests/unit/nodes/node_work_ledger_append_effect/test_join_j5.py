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
    LIVE_LEDGER_FILE_NAMES,
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


def _request(cid: str, ref: str = IDENTITY_REF) -> ModelJoinJ5Request:
    request_id = uuid4()
    return ModelJoinJ5Request(
        correlation_id=cid,
        identity_ref=ref,
        append=ModelWorkLedgerAppendRequest(
            request_id=request_id,
            ledger_id=LEDGER_ID,
            rows=_row_text(request_id),
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
    ) -> None:
        self.projection = InMemoryProjectedRows()
        self.heard = InMemoryProjectedRows()
        self.ledger = InMemoryJoinLedger(
            projection=self.projection if projection_hears_ledger else self.heard
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
                    names_failed_answers=True,
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
    live_name = next(iter(LIVE_LEDGER_FILE_NAMES))
    with pytest.raises(ValueError, match="live ledger"):
        ScratchFileJoinLedger(tmp_path / live_name, projection=InMemoryProjectedRows())
    assert not (tmp_path / live_name).exists()


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


def test_join_j5_error_a_piece_6_failure_after_a_piece_5_success_reads_broken_on_the_floor() -> (
    None
):
    """Piece 5 answered and piece 6 then failed: the floor reads the join ``broken`` and names the break."""
    rig = _Rig(projection_hears_ledger=False)
    cid = uuid4().hex
    rig.upstream_chain(cid, resolves=True)

    rig.join.handle(_request(cid))

    state, _, findings = _fold(rig.sink.events, NOW)
    assert (state, findings) == (
        "broken",
        ["downstream_failed_after_upstream_succeeded"],
    )


def test_control_a_landed_answer_after_a_piece_5_success_is_not_a_finding() -> None:
    """Piece 5 answered and piece 6 landed: the floor reads ``golden_only`` and names no break."""
    rig = _Rig()
    cid = uuid4().hex
    rig.upstream_chain(cid, resolves=True)

    rig.join.handle(_request(cid))

    state, _, findings = _fold(rig.sink.events, NOW)
    assert (state, findings) == ("golden_only", [])


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


# --- review findings on PR 5286 (OMN-22914): each test failed on the branch before its fix ---------------------------


def _request_with_rows(
    cid: str, rows: str, append_if_absent_row_id: str | None = None
) -> ModelJoinJ5Request:
    """A request whose rows text is the caller's; ``{rid}`` in it is the request id."""
    request_id = uuid4()
    return ModelJoinJ5Request(
        correlation_id=cid,
        identity_ref=IDENTITY_REF,
        append=ModelWorkLedgerAppendRequest(
            request_id=request_id,
            ledger_id=LEDGER_ID,
            rows=rows.replace("{rid}", str(request_id)),
            requested_by_lane="j5-test",
            requesting_host="test-host",
            requested_at=NOW,
            append_if_absent_row_id=append_if_absent_row_id,
        ),
    )


_TWO_ROWS = (
    "2026-11-07T12:00:00Z | STATUS | lane=j5-test | req={rid} | first row\n"
    "2026-11-07T12:00:01Z | STATUS | lane=j5-test | req={rid} | second row"
)


def test_join_j5_every_landed_row_is_read_back_and_counted() -> None:
    """Failed before: the join verified only ledger_lines[0], so ``rows_appended`` read 1 for a two-row append."""
    rig = _Rig()
    cid = uuid4().hex
    rig.upstream_chain(cid, resolves=True)

    result = rig.join.handle(_request_with_rows(cid, _TWO_ROWS))

    assert result.outcome is EnumJoinJ5Outcome.LANDED
    mine = [e for e in rig.sink.events if e.piece == POLICY.to_piece]
    assert mine[-1].counters == {"rows_appended": 2, "rows_projected": 2}


def test_join_j5_a_later_landed_row_the_projection_never_heard_fails_typed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Failed before: ``assert result.outcome is PROJECTED_ROW_MISSING`` saw LANDED, the second row was never read."""
    rig = _Rig()
    cid = uuid4().hex
    rig.upstream_chain(cid, resolves=True)
    project = rig.projection.project

    def drop_second(*, raw_row: str, ledger_id: str, ledger_seq: int) -> None:
        if "second row" not in raw_row:
            project(raw_row=raw_row, ledger_id=ledger_id, ledger_seq=ledger_seq)

    monkeypatch.setattr(rig.projection, "project", drop_second)

    result = rig.join.handle(_request_with_rows(cid, _TWO_ROWS))

    assert result.outcome is EnumJoinJ5Outcome.PROJECTED_ROW_MISSING
    mine = [e for e in rig.sink.events if e.piece == POLICY.to_piece]
    assert [e.phase for e in mine] == ["start", "failure"]


_ROW_WITH_CONTINUATION = (
    "2026-11-07T12:00:00Z | STATUS | lane=j5-test | req={rid} | header\n"
    "    continuation line one\n"
    "    continuation line two"
)


def test_join_j5_projected_row_keeps_the_continuation_lines_of_the_durable_row() -> (
    None
):
    """Failed before: ``assert 'continuation line one' in projected.raw_row`` (only the header line was projected)."""
    rig = _Rig()
    cid = uuid4().hex
    rig.upstream_chain(cid, resolves=True)

    result = rig.join.handle(_request_with_rows(cid, _ROW_WITH_CONTINUATION))

    assert result.outcome is EnumJoinJ5Outcome.LANDED
    projected = rig.projection.read_row(
        ledger_id=LEDGER_ID, ledger_seq=result.ledger_seq
    )
    assert projected is not None
    assert "continuation line one" in projected.raw_row
    assert projected.raw_row.endswith("    continuation line two")


def test_join_j5_a_projection_that_lost_the_continuation_lines_is_not_landed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Failed before: ``assert result.outcome is PROJECTED_ROW_MISSING`` saw LANDED (header-only comparison)."""
    rig = _Rig()
    cid = uuid4().hex
    rig.upstream_chain(cid, resolves=True)
    project = rig.projection.project

    def header_only(*, raw_row: str, ledger_id: str, ledger_seq: int) -> None:
        project(
            raw_row=raw_row.splitlines()[0], ledger_id=ledger_id, ledger_seq=ledger_seq
        )

    monkeypatch.setattr(rig.projection, "project", header_only)

    result = rig.join.handle(_request_with_rows(cid, _ROW_WITH_CONTINUATION))

    assert result.outcome is EnumJoinJ5Outcome.PROJECTED_ROW_MISSING


@pytest.mark.parametrize("backend", ["memory", "file"])
def test_join_j5_a_header_without_a_final_newline_does_not_swallow_the_row(
    backend: str, tmp_path: Path
) -> None:
    """Failed before: the row was glued onto the header line, so ``assert result.outcome is LANDED`` saw a refusal."""
    projection = InMemoryProjectedRows()
    ledger: InMemoryJoinLedger | ScratchFileJoinLedger
    if backend == "memory":
        ledger = InMemoryJoinLedger(projection=projection, header="# header, no eol")
    else:
        scratch = tmp_path / "scratch-no-eol.md"
        scratch.write_text("# header, no eol", encoding="utf-8")
        ledger = ScratchFileJoinLedger(scratch, projection=projection)
    sink = InMemoryEmissionSink()
    join = HandlerWorkLedgerJoinJ5(
        append=HandlerWorkLedgerAppendEffect(
            ledger,
            ledger,
            "test-host",
            writer_principals=(STORE_PRINCIPAL,),
            operator_principal="operator",
        ),
        ledger=ledger,
        identities=InMemoryStoreIdentities({IDENTITY_REF: STORE_PRINCIPAL}),
        projection=projection,
        sink=sink,
        policy=POLICY,
        clock=_Clock(),
        backend="in_memory",
    )

    result = join.handle(_request(uuid4().hex))

    assert result.outcome is EnumJoinJ5Outcome.LANDED
    lines = ledger.read_text().splitlines()
    assert lines[0] == "# header, no eol"
    assert lines[result.ledger_seq - 1].startswith("2026-11-07T12:00:00Z | STATUS")


def test_join_j5_a_replayed_request_emits_no_second_start_or_success() -> None:
    """Failed before: the replay emitted a second start and success, ``[start, success, start, success]``."""
    rig = _Rig()
    cid = uuid4().hex
    rig.upstream_chain(cid, resolves=True)
    request = _request(cid)

    first = rig.join.handle(request)
    second = rig.join.handle(request)

    assert first.outcome is EnumJoinJ5Outcome.LANDED
    assert second.receipt is not None
    assert second.receipt.status is EnumWorkLedgerAppendStatus.DUPLICATE
    mine = [e for e in rig.sink.events if e.piece == POLICY.to_piece]
    assert [e.phase for e in mine] == ["start", "success"]
    state, _golden, findings = _fold(rig.sink.events, NOW)
    assert (state, findings) == ("golden_only", [])


def test_join_j5_an_id_already_held_by_another_request_is_not_a_landed_row() -> None:
    """Failed before: ``assert second.outcome is APPEND_NOT_ACCEPTED`` saw LANDED (ALREADY_PRESENT counted as durable)."""
    rig = _Rig()
    append = HandlerWorkLedgerAppendEffect(
        rig.ledger,
        rig.ledger,
        "test-host",
        writer_principals=(STORE_PRINCIPAL,),
        operator_principal="operator",
        append_if_absent=ModelAppendIfAbsentPolicy(
            row_id_cell="id", lock_suffix=".test-append.lock", lock_wait_seconds=30
        ),
    )
    join = HandlerWorkLedgerJoinJ5(
        append=append,
        ledger=rig.ledger,
        identities=rig.store,
        projection=rig.projection,
        sink=rig.sink,
        policy=POLICY,
        clock=rig.clock,
        backend="in_memory",
    )
    row = "2026-11-07T12:00:00Z | STATUS | lane=j5-test | id=row-x | req={rid} | "
    first = join.handle(
        _request_with_rows(
            uuid4().hex, row + "first text", append_if_absent_row_id="row-x"
        )
    )
    cid = uuid4().hex
    rig.upstream_chain(cid, resolves=True)

    second = join.handle(
        _request_with_rows(cid, row + "other text", append_if_absent_row_id="row-x")
    )

    assert first.outcome is EnumJoinJ5Outcome.LANDED
    assert second.receipt is not None
    assert second.receipt.status is EnumWorkLedgerAppendStatus.ALREADY_PRESENT
    assert second.outcome is EnumJoinJ5Outcome.APPEND_NOT_ACCEPTED
    assert second.failure_code == POLICY.append_not_accepted_failure_code
    assert "other text" not in rig.ledger.read_text()


def test_join_j5_a_scratch_name_that_links_to_the_live_ledger_is_refused(
    tmp_path: Path,
) -> None:
    """Failed before: ``pytest.raises(ValueError)`` did not raise for a symlink named like a scratch file."""
    live = tmp_path / sorted(LIVE_LEDGER_FILE_NAMES)[0]
    live.write_text("# live ledger\n", encoding="utf-8")
    link = tmp_path / "scratch-link.md"
    link.symlink_to(live)

    with pytest.raises(ValueError, match="live ledger"):
        ScratchFileJoinLedger(link, projection=InMemoryProjectedRows())

    assert live.read_text(encoding="utf-8") == "# live ledger\n"


def test_join_j5_a_row_with_trailing_whitespace_lands_and_reads_back_equal() -> None:
    """Failed before: ``assert result.outcome is LANDED`` saw PROJECTED_ROW_MISSING (the projection trimmed the row)."""
    rig = _Rig()
    cid = uuid4().hex
    rig.upstream_chain(cid, resolves=True)

    result = rig.join.handle(
        _request_with_rows(
            cid,
            "2026-11-07T12:00:00Z | STATUS | lane=j5-test | req={rid} | probe row   ",
        )
    )

    assert result.outcome is EnumJoinJ5Outcome.LANDED
    projected = rig.projection.read_row(
        ledger_id=LEDGER_ID, ledger_seq=result.ledger_seq
    )
    assert projected is not None
    assert (
        projected.raw_row == rig.ledger.read_text().splitlines()[result.ledger_seq - 1]
    )
    assert projected.raw_row.endswith("probe row   ")
