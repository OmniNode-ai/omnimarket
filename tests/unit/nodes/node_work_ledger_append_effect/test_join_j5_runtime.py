# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""J5 runner (OMN-23086): golden and error chain event tests and the failing controls, judged by the floor's own fold.

Before this run the J5 join handler (OMN-22914) was called by nothing: piece 5's readings were chains of the floor's own
(ledger rows under the floor's piece event rule), piece 6 never answered them, and the floor read J5 ``broken``. The
runner answers each piece 5 golden reading with an append under the store-resolved identity and piece 6's chain; the join
refusal node answers each piece 5 failure. The golden case reads ``golden_only`` and, with the refusal, ``passed``.

Failing controls: the run removed leaves the join broken (the reading the floor showed); a projection that never hears the
row, and a store that resolves nothing, each leave the join not passed with the typed failure on the journal.
"""

from __future__ import annotations

import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest

from omnimarket.handlers.emission_sink import InMemoryEmissionSink
from omnimarket.models.floor_17a import (
    ModelFloorReceiptInput,
    ModelJoinDef,
    ModelNodeEmission,
    ModelPieceDef,
    ModelPieceEventRule,
)
from omnimarket.models.model_reading import ModelLedgerEvidenceRow
from omnimarket.models.work_ledger_append import (
    EnumJoinJ5Outcome,
    EnumWorkLedgerAppendStatus,
    ModelJoinJ5RunCommand,
    ModelWorkLedgerAppendReceipt,
    ModelWorkLedgerAppendRequest,
)
from omnimarket.nodes.node_floor_emission_compute.handlers.handler_floor_receipt import (
    HandlerFloorReceipt,
)
from omnimarket.nodes.node_floor_simple_effect.handlers.ledger_reads import (
    piece_events,
)
from omnimarket.nodes.node_join_refusal_effect import HandlerJoinRefusal
from omnimarket.nodes.node_join_refusal_effect.models import (
    ModelJoinRefusalCommand,
)
from omnimarket.nodes.node_join_refusal_effect.overlay import (
    ModelJoinRefusalOverlay,
)
from omnimarket.nodes.node_reading_writer_effect.adapters.ledger_adapters import (
    CommandLedgerRowSource,
    InMemoryLedger,
    evidence_row_from_text,
)
from omnimarket.nodes.node_reading_writer_effect.protocols import (
    ModelLedgerQuery,
    ModelLedgerRead,
)
from omnimarket.nodes.node_secret_reading_effect.adapters import (
    InMemorySecretProbeStore,
)
from omnimarket.nodes.node_work_ledger_append_effect.adapters import (
    BusCliAppend,
    ModelStoreIdentityBinding,
    SourceProjectedRows,
    StoreProbeIdentities,
)
from omnimarket.nodes.node_work_ledger_append_effect.handlers.handler_work_ledger_append_effect import (
    HandlerWorkLedgerAppendEffect,
)
from omnimarket.nodes.node_work_ledger_append_effect.handlers.handler_work_ledger_join_j5 import (
    HandlerWorkLedgerJoinJ5,
    load_join_j5_policy,
)
from omnimarket.nodes.node_work_ledger_append_effect.handlers.handler_work_ledger_join_j5_run import (
    HandlerWorkLedgerJoinJ5Run,
)
from omnimarket.nodes.node_work_ledger_append_effect.overlay_join_j5 import (
    ModelJoinJ5Overlay,
)
from omnimarket.nodes.node_work_ledger_append_effect.protocols.join_j5 import (
    InMemoryJoinLedger,
    InMemoryProjectedRows,
    StoreIdentityUnresolvedError,
)

pytestmark = pytest.mark.unit

NOW = datetime(2026, 11, 8, 12, 0, 0, tzinfo=UTC)
POLICY = load_join_j5_policy()
LANE = "example-secret-reading"
LEDGER_ID = "rolling-work-ledger"
BINDING = ModelStoreIdentityBinding(
    ref="example-ledger-writer",
    folder="/shared/env",
    key="EXAMPLE_LEDGER_WRITER_KEY",
    principal="example-writer",
)
RULE = ModelPieceEventRule.model_validate(
    {
        "piece": 5,
        "node": "example_secret_node:read",
        "golden": {
            "row_type": "STATUS",
            "lane": LANE,
            "cells": {
                "src": LANE,
                "drill": "no",
                "verdict": "WORKING",
                "outcome": "read",
            },
        },
        "error": {
            "row_type": "STATUS",
            "lane": LANE,
            "cells": {"src": LANE, "verdict": "NOT_WORKING"},
        },
        "failure_code_cell": "reason",
        "failure_default": "secret_reading_failed",
        "error_drill_cell": "drill",
        "lookback_days": 7,
    }
)


class _Clock:
    def __init__(self, start: datetime) -> None:
        self._t = start

    def __call__(self) -> datetime:
        self._t += timedelta(seconds=1)
        return self._t


def _reading_row(at: datetime, verdict: str, *, drill: str = "no") -> str:
    stamp = at.strftime("%Y-%m-%dT%H:%M:%SZ")
    return (
        f"{stamp} | STATUS | lane={LANE} | src={LANE} | verdict={verdict} | outcome=read"
        f" | reason=example_failure | drill={drill} | probed_at={stamp}"
    )


class _Rig:
    """Piece 5's rows on the ledger source, the append and projection in memory, the store with the identity's key."""

    def __init__(
        self,
        rows: list[str],
        *,
        projection_hears_ledger: bool = True,
        store_has_key: bool = True,
    ) -> None:
        self.source = InMemoryLedger(rows)
        self.projected = InMemoryProjectedRows()
        self.deaf = InMemoryProjectedRows()
        self.ledger = InMemoryJoinLedger(
            projection=self.projected if projection_hears_ledger else self.deaf
        )
        self.sink = InMemoryEmissionSink()
        self.sleeps: list[float] = []
        self.store = InMemorySecretProbeStore(
            {(BINDING.folder, BINDING.key): "x"} if store_has_key else {}
        )
        self.clock = _Clock(NOW)

    def runner(self, *, max_appends: int = 1) -> HandlerWorkLedgerJoinJ5Run:
        append = HandlerWorkLedgerAppendEffect(
            self.ledger,
            self.ledger,
            "test-host",
            writer_principals=(BINDING.principal,),
            operator_principal="operator",
        )
        join = HandlerWorkLedgerJoinJ5(
            append=append,
            ledger=self.ledger,
            identities=StoreProbeIdentities(self.store, (BINDING,)),
            projection=SourceProjectedRows(
                self.source_of_projection(),
                attempts=3,
                interval_seconds=0.5,
                slack_seconds=60,
                sleep=self.sleeps.append,
                clock=lambda: NOW + timedelta(hours=1),
            ),
            sink=self.sink,
            policy=POLICY,
            clock=self.clock,
            backend="in_memory",
        )
        return HandlerWorkLedgerJoinJ5Run(
            join=join,
            source=self.source,
            sink=self.sink,
            upstream=RULE,
            identity_ref=BINDING.ref,
            ledger_id=LEDGER_ID,
            row_lane="example-join-j5",
            requesting_lane="example-join-j5",
            row_text="piece 6 appended a row for a piece 5 reading",
            host="test-host",
            window_hours=48,
            max_appends=max_appends,
            to_piece=POLICY.to_piece,
            clock=lambda: NOW + timedelta(minutes=1),
        )

    def source_of_projection(self) -> InMemoryLedger:
        """The projection as a row source: it holds the lines the projection heard."""
        return _ProjectionSource(self.projected, self.deaf, self.ledger)


class _ProjectionSource(InMemoryLedger):
    """The read command over the projection: it answers with the rows the projection heard, by text."""

    def __init__(
        self,
        heard: InMemoryProjectedRows,
        deaf: InMemoryProjectedRows,
        ledger: InMemoryJoinLedger,
    ) -> None:
        super().__init__()
        self._heard = heard
        self._ledger = ledger

    def read(self, query: ModelLedgerQuery) -> ModelLedgerRead:
        lines = [r.raw_row for r in self._heard.rows()]
        self.lines = lines
        return super().read(query)


def _fold(emissions: list[ModelNodeEmission], now: datetime) -> tuple[str, list[str]]:
    receipts = HandlerFloorReceipt().handle(
        ModelFloorReceiptInput(
            now=now,
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
            chain_deadline_s=900,
        )
    )
    join = next(j for j in receipts.joins if j.join == POLICY.join_id)
    return join.state, [f.code for f in receipts.findings]


def _floor_read(rig: _Rig, now: datetime) -> tuple[str, list[str]]:
    """What the floor folds: piece 5's chains from the ledger rows, plus every emission on the journal."""
    rows = [r for line in rig.source.lines if (r := _evidence(line)) is not None]
    a_chains = piece_events(rows, (RULE,))
    return _fold([*a_chains, *rig.sink.events], now)


def _evidence(line: str) -> ModelLedgerEvidenceRow | None:
    return evidence_row_from_text(line)


def _refuse(rig: _Rig, now: datetime) -> None:
    """The join refusal node, as the publish cycle runs it, over the same rows and the same journal."""
    overlay = ModelJoinRefusalOverlay.model_validate(
        {
            "ledger_read": {
                "route": "file",
                "path_env": "UNUSED",
                "timeout_seconds": 60,
                "row_limit": 5000,
            },
            "emission": {
                "home_env": "UNUSED",
                "journal_relpath": "unused.jsonl",
                "window_hours": 48,
            },
            "joins": [
                {
                    "join": "J5",
                    "from_piece": 5,
                    "to_piece": 6,
                    "downstream_node": "example_ledger_node:join_j5",
                    "injected_fault": "Drop the piece 6 refusal from the emission journal",
                    "upstream": RULE.model_dump(mode="json"),
                }
            ],
        }
    )
    HandlerJoinRefusal(
        overlay=overlay,
        source=rig.source,
        sink=rig.sink,
        backend="in_memory",
        clock=lambda: now,
    ).handle(ModelJoinRefusalCommand(write=True))


GOLDEN_AT = NOW - timedelta(minutes=30)
ERROR_AT = NOW - timedelta(minutes=45)


# --- the golden chain ------------------------------------------------------------------------------------------------


def test_golden_run_answers_each_piece_5_reading_and_the_floor_reads_golden_only() -> (
    None
):
    rig = _Rig([_reading_row(GOLDEN_AT, "WORKING")])

    result = rig.runner().handle(ModelJoinJ5RunCommand())

    assert result.read_ok
    assert (result.owed, result.answered) == (1, 0)
    assert [i.outcome for i in result.items] == [EnumJoinJ5Outcome.LANDED]
    # The append is on the durable text, once, under the store-resolved principal, and the projection heard it.
    assert len(rig.ledger.append_calls) == 1
    assert f"principal={BINDING.principal}" in rig.ledger.read_text()
    assert result.items[0].ledger_seq is not None
    # Piece 6's chain sits on the journal under the id of piece 5's chain: a start, then a success.
    phases = [e.phase for e in rig.sink.events if e.piece == POLICY.to_piece]
    assert phases == ["start", "success"]
    state, findings = _floor_read(rig, NOW + timedelta(minutes=5))
    assert (state, findings) == ("golden_only", [])


def test_golden_and_error_together_read_passed() -> None:
    rig = _Rig(
        [_reading_row(GOLDEN_AT, "WORKING"), _reading_row(ERROR_AT, "NOT_WORKING")]
    )

    rig.runner().handle(ModelJoinJ5RunCommand())
    _refuse(rig, NOW + timedelta(minutes=2))

    assert _floor_read(rig, NOW + timedelta(minutes=5)) == ("passed", [])


def test_a_rerun_appends_nothing_for_a_chain_piece_6_already_answered() -> None:
    rig = _Rig([_reading_row(GOLDEN_AT, "WORKING")])
    runner = rig.runner()
    runner.handle(ModelJoinJ5RunCommand())

    again = runner.handle(ModelJoinJ5RunCommand())

    assert (again.owed, again.answered, again.items) == (0, 1, ())
    assert len(rig.ledger.append_calls) == 1


def test_a_run_answers_at_most_the_overlay_bound_newest_first() -> None:
    older = GOLDEN_AT - timedelta(hours=2)
    rig = _Rig([_reading_row(older, "WORKING"), _reading_row(GOLDEN_AT, "WORKING")])

    result = rig.runner(max_appends=1).handle(ModelJoinJ5RunCommand())

    assert (result.owed, len(result.items)) == (2, 1)
    newest_row = _evidence(rig.source.lines[1])
    assert newest_row is not None
    newest = piece_events([newest_row], (RULE,))[0]
    assert result.items[0].correlation_id == newest.correlation_id


# --- the error chain ---------------------------------------------------------------------------------------------------


def test_error_a_drill_reading_and_a_failed_reading_are_never_answered_by_the_runner() -> (
    None
):
    rig = _Rig(
        [
            _reading_row(ERROR_AT, "NOT_WORKING"),
            _reading_row(ERROR_AT, "NOT_WORKING", drill="yes"),
        ]
    )

    result = rig.runner().handle(ModelJoinJ5RunCommand())

    assert (result.owed, result.items) == (0, ())
    assert rig.ledger.append_calls == []
    assert rig.sink.events == []


def test_error_an_unreadable_ledger_is_not_an_empty_answer() -> None:
    rig = _Rig([_reading_row(GOLDEN_AT, "WORKING")])
    rig.source.read_error = "ledger-file-unreadable:OSError"

    result = rig.runner().handle(ModelJoinJ5RunCommand())

    assert result.read_ok is False
    assert "unread" in result.reason
    assert rig.ledger.append_calls == []


def test_a_probe_reads_what_is_owed_and_writes_nothing() -> None:
    rig = _Rig([_reading_row(GOLDEN_AT, "WORKING")])

    result = rig.runner().handle(ModelJoinJ5RunCommand(probe=True))

    assert (result.owed, result.items) == (1, ())
    assert rig.ledger.append_calls == []
    assert rig.sink.events == []


# --- the failing controls ----------------------------------------------------------------------------------------------


def test_control_without_the_run_and_the_refusal_the_join_reads_broken() -> None:
    """The reading the floor showed: piece 5 failed, piece 6 was silent past the deadline."""
    rig = _Rig([_reading_row(ERROR_AT, "NOT_WORKING")])

    state, findings = _floor_read(rig, NOW + timedelta(hours=1))

    assert state == "broken"
    assert findings == ["downstream_missing_typed_failure"]


def test_control_a_projection_that_never_hears_the_row_leaves_the_join_not_passed() -> (
    None
):
    rig = _Rig(
        [_reading_row(GOLDEN_AT, "WORKING"), _reading_row(ERROR_AT, "NOT_WORKING")],
        projection_hears_ledger=False,
    )

    result = rig.runner().handle(ModelJoinJ5RunCommand())
    _refuse(rig, NOW + timedelta(minutes=2))

    assert [i.outcome for i in result.items] == [
        EnumJoinJ5Outcome.PROJECTED_ROW_MISSING
    ]
    assert result.items[0].failure_code == POLICY.projected_row_missing_failure_code
    # The projection was asked again for the attempts the overlay sets, waiting between them.
    assert rig.sleeps == [0.5, 0.5]
    state, _ = _floor_read(rig, NOW + timedelta(minutes=5))
    assert state == "error_only"


def test_control_a_store_without_the_identity_key_leaves_the_join_not_passed() -> None:
    rig = _Rig([_reading_row(GOLDEN_AT, "WORKING")], store_has_key=False)

    result = rig.runner().handle(ModelJoinJ5RunCommand())

    assert [i.outcome for i in result.items] == [
        EnumJoinJ5Outcome.REFUSED_IDENTITY_UNRESOLVED
    ]
    assert rig.ledger.append_calls == []
    state, _ = _floor_read(rig, NOW + timedelta(minutes=5))
    assert state == "not_run"


# --- the real ports ----------------------------------------------------------------------------------------------------


def test_store_identities_resolve_only_when_the_store_resolves_the_key() -> None:
    store = InMemorySecretProbeStore({(BINDING.folder, BINDING.key): "x"})
    identities = StoreProbeIdentities(store, (BINDING,))

    assert identities.resolve(BINDING.ref) == BINDING.principal
    store.revoke(BINDING.ref)
    with pytest.raises(StoreIdentityUnresolvedError, match="refused"):
        identities.resolve(BINDING.ref)
    with pytest.raises(StoreIdentityUnresolvedError, match="no store binding"):
        identities.resolve("another-ref")
    store.down = True
    with pytest.raises(StoreIdentityUnresolvedError):
        StoreProbeIdentities(store, (BINDING,)).resolve(BINDING.ref)


def test_projected_rows_read_by_text_and_an_error_reads_as_no_row() -> None:
    row = _reading_row(GOLDEN_AT, "WORKING")
    found = SourceProjectedRows(
        InMemoryLedger([row]),
        attempts=1,
        interval_seconds=0.1,
        slack_seconds=60,
        clock=lambda: NOW,
    ).read_row(ledger_id=LEDGER_ID, ledger_seq=7, raw_row=row)
    assert found is not None
    assert (found.raw_row, found.ledger_seq) == (row, 7)

    broken = InMemoryLedger([row])
    broken.read_error = "read-command-exit-1"
    assert (
        SourceProjectedRows(
            broken,
            attempts=2,
            interval_seconds=0.1,
            slack_seconds=60,
            sleep=lambda _s: None,
            clock=lambda: NOW,
        ).read_row(ledger_id=LEDGER_ID, ledger_seq=7, raw_row=row)
        is None
    )
    assert (
        SourceProjectedRows(
            InMemoryLedger([row]),
            attempts=1,
            interval_seconds=0.1,
            slack_seconds=60,
            clock=lambda: NOW,
        ).read_row(ledger_id=LEDGER_ID, ledger_seq=7)
        is None
    )


def test_the_example_overlay_loads_and_holds_no_value() -> None:
    import yaml

    example = (
        Path(__file__).resolve().parents[4]
        / "src/omnimarket/nodes/node_work_ledger_append_effect/overlay_join_j5.example.yaml"
    )
    overlay = ModelJoinJ5Overlay.model_validate(
        yaml.safe_load(example.read_text(encoding="utf-8"))
    )
    assert overlay.identity.ref == BINDING.ref
    assert overlay.allow_injected_break is False


# --- the real append and the durable read-back -------------------------------------------------------------------------


def _receipt_json(
    request_id: object, *, principal: str, status: str = "accepted"
) -> str:
    return ModelWorkLedgerAppendReceipt(
        request_id=request_id,
        status=EnumWorkLedgerAppendStatus(status),
        exit_code=0,
        message="ok",
        ledger_lines=[3],
        ledger_host="ledger-host",
        duration_ms=5,
        principal=principal,
    ).model_dump_json()


class _Cli:
    """A stand-in for the work-ledger bus append command: records its argv and prints one receipt."""

    def __init__(self, principal: str, status: str = "accepted") -> None:
        self.principal = principal
        self.status = status
        self.argv: list[str] = []
        self.rows_seen = ""

    def __call__(
        self, argv: list[str], **_kw: object
    ) -> subprocess.CompletedProcess[str]:
        self.argv = argv
        self.rows_seen = Path(argv[argv.index("--rows-file") + 1]).read_text(
            encoding="utf-8"
        )
        request_id = argv[argv.index("--request-id") + 1]
        return subprocess.CompletedProcess(
            argv,
            0,
            stdout="noise\n"
            + _receipt_json(request_id, principal=self.principal, status=self.status)
            + "\n",
            stderr="",
        )


def _append_request() -> ModelWorkLedgerAppendRequest:
    request_id = uuid4()
    return ModelWorkLedgerAppendRequest(
        request_id=request_id,
        ledger_id=LEDGER_ID,
        rows=f"2026-11-08T12:00:00Z | STATUS | lane=example-join-j5 | req={request_id} | row",
        requested_by_lane="example-join-j5",
        requesting_host="test-host",
        requested_at=NOW,
    )


def test_bus_append_accepts_a_receipt_authenticated_as_the_store_identity() -> None:
    cli = _Cli(BINDING.principal)
    append = BusCliAppend(
        ["cmd", "append"], host="test-host", timeout_seconds=30, run=cli
    )
    request = _append_request()

    receipt = append.handle(request, writer_principal=BINDING.principal)

    assert receipt.status is EnumWorkLedgerAppendStatus.ACCEPTED
    assert receipt.ledger_lines == [3]
    assert cli.rows_seen == request.rows
    assert cli.argv[:2] == ["cmd", "append"]
    assert request.rows not in cli.argv


def test_control_bus_append_authenticated_as_someone_else_is_not_an_accepted_append() -> (
    None
):
    """The break: the broker login is not the identity the store resolved; the receipt's own status says accepted."""
    cli = _Cli("another-principal")
    append = BusCliAppend(
        ["cmd", "append"], host="test-host", timeout_seconds=30, run=cli
    )

    receipt = append.handle(_append_request(), writer_principal=BINDING.principal)

    assert receipt.status is EnumWorkLedgerAppendStatus.REFUSED
    assert "not the identity the store resolved" in receipt.message


def test_bus_append_with_no_receipt_or_a_failed_command_is_an_error_receipt() -> None:
    def silent(argv: list[str], **_kw: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(argv, 69, stdout="", stderr="bus: down")

    def missing(argv: list[str], **_kw: object) -> subprocess.CompletedProcess[str]:
        raise FileNotFoundError(argv[0])

    for run in (silent, missing):
        receipt = BusCliAppend(
            ["cmd"], host="test-host", timeout_seconds=30, run=run
        ).handle(_append_request(), writer_principal=BINDING.principal)
        assert receipt.status is EnumWorkLedgerAppendStatus.ERROR


def test_control_a_receipt_naming_a_line_the_ledger_does_not_have_is_not_a_landing() -> (
    None
):
    """The ledger the join reads back has not caught up with the receipt: the append is not counted as landed."""
    rig = _Rig([_reading_row(GOLDEN_AT, "WORKING")])
    cli = _Cli(BINDING.principal)
    join = HandlerWorkLedgerJoinJ5(
        append=BusCliAppend(["cmd"], host="test-host", timeout_seconds=30, run=cli),
        ledger=InMemoryJoinLedger(projection=InMemoryProjectedRows()),
        identities=StoreProbeIdentities(rig.store, (BINDING,)),
        projection=InMemoryProjectedRows(),
        sink=rig.sink,
        policy=POLICY,
        clock=rig.clock,
        backend="in_memory",
    )
    runner = rig.runner()
    runner._join = join

    result = runner.handle(ModelJoinJ5RunCommand())

    assert [i.outcome for i in result.items] == [EnumJoinJ5Outcome.APPEND_NOT_ACCEPTED]
    assert [e.phase for e in rig.sink.events if e.piece == POLICY.to_piece] == [
        "start",
        "failure",
    ]


def test_control_a_ledger_that_holds_another_row_at_the_receipt_position_is_not_a_landing() -> (
    None
):
    """The break found on the first lab run: a stale copy read a different row at the position, the projection held that row,
    and the join read a landing for an append whose own row it never saw."""
    rig = _Rig([_reading_row(GOLDEN_AT, "WORKING")])
    stale = InMemoryJoinLedger(projection=InMemoryProjectedRows())
    stale.append(
        "2026-11-08T11:00:00Z | STATUS | lane=other | req=11111111-1111-4111-8111-111111111111 | x"
    )
    stale.append(
        "2026-11-08T11:00:01Z | STATUS | lane=other | req=22222222-2222-4222-8222-222222222222 | y"
    )
    stale.append(
        "2026-11-08T11:00:02Z | STATUS | lane=other | req=33333333-3333-4333-8333-333333333333 | z"
    )
    heard = InMemoryProjectedRows()
    for seq, line in enumerate(stale.read_text().splitlines(), start=1):
        heard.project(raw_row=line, ledger_id=LEDGER_ID, ledger_seq=seq)
    join = HandlerWorkLedgerJoinJ5(
        append=BusCliAppend(
            ["cmd"], host="test-host", timeout_seconds=30, run=_Cli(BINDING.principal)
        ),
        ledger=stale,
        identities=StoreProbeIdentities(rig.store, (BINDING,)),
        projection=heard,
        sink=rig.sink,
        policy=POLICY,
        clock=rig.clock,
        backend="in_memory",
    )
    runner = rig.runner()
    runner._join = join

    result = runner.handle(ModelJoinJ5RunCommand())

    assert [i.outcome for i in result.items] == [EnumJoinJ5Outcome.APPEND_NOT_ACCEPTED]
    state, _ = _floor_read(rig, NOW + timedelta(minutes=5))
    assert state == "not_run"


# --- the reader's contract: the projection read carries the join run's correlation id (OMN-23086) ----------------------
#
# Recorded (h053, 2026-11-08, after the 09:07Z install): the J5 append landed at ledger line 4698 and the projection held
# the row, yet the step exited 1 with ``projected_row_missing``. The projection read goes to ``onex ledger query --stdin``,
# whose request model (``ModelWorkLedgerQueryRequest``) requires a ``correlation_id``; the J5 row source never sent one, so
# the reader refused every read, and a refused read reads as no projected row. The stub below validates each request with
# the reader's own model, so it refuses exactly as the real reader did.

_CONTRACT_READER = """
import json, sys
from pydantic import ValidationError
from omnimarket.models.work_ledger_query.model_work_ledger_query import ModelWorkLedgerQueryRequest
rows_file = sys.argv[1]
try:
    request = ModelWorkLedgerQueryRequest.model_validate_json(sys.stdin.read())
except ValidationError as exc:
    sys.stderr.write("Error: Work-ledger read refused (ValidationError)\\n")
    sys.exit(1)
rows = [
    {"raw_row": line, "projected_at": "2026-11-08T12:00:30Z"}
    for line in open(rows_file, encoding="utf-8").read().splitlines()
    if line.strip()
]
print(json.dumps({"status": "ok", "correlation_id": str(request.correlation_id), "rows": rows, "matched": len(rows)}))
"""


class _WireProjection:
    """The projection as the real row source reaches it: the rows the projection heard go to a file, and the read command
    (the reader's contract, run as a process) answers from it. Every read crosses the same wire the host's does."""

    def __init__(self, heard: InMemoryProjectedRows, rows_file: Path) -> None:
        self._heard = heard
        self._file = rows_file
        self.reader = CommandLedgerRowSource(
            [sys.executable, "-c", _CONTRACT_READER, str(rows_file)], 60, 5000
        )

    def read(self, query: ModelLedgerQuery) -> ModelLedgerRead:
        self._file.write_text(
            "\n".join(r.raw_row for r in self._heard.rows()) + "\n", encoding="utf-8"
        )
        return self.reader.read(query)


def _wire_runner(rig: _Rig, wire: _WireProjection) -> HandlerWorkLedgerJoinJ5Run:
    runner = rig.runner()
    runner._join._projection = SourceProjectedRows(
        wire,
        attempts=2,
        interval_seconds=0.1,
        slack_seconds=60,
        sleep=lambda _s: None,
        clock=lambda: NOW + timedelta(hours=1),
    )
    return runner


def test_the_reader_refuses_a_query_that_carries_no_correlation_id(
    tmp_path: Path,
) -> None:
    """The control for the stub: the recorded rejection, read through the real command route, with nothing sent."""
    rows_file = tmp_path / "rows.txt"
    rows_file.write_text(_reading_row(GOLDEN_AT, "WORKING") + "\n", encoding="utf-8")
    reader = CommandLedgerRowSource(
        [sys.executable, "-c", _CONTRACT_READER, str(rows_file)], 60, 5000
    )
    query = ModelLedgerQuery(
        since=GOLDEN_AT - timedelta(minutes=5),
        until=NOW,
        row_type="STATUS",
    )

    refused = reader.read(query)
    assert refused.error == "read-command-exit-1:ValidationError"
    assert refused.rows == ()
    answered = reader.read(query.model_copy(update={"correlation_id": uuid4().hex}))
    assert answered.error is None
    assert len(answered.rows) == 1


def test_the_projection_read_sends_the_correlation_id_the_reader_requires(
    tmp_path: Path,
) -> None:
    """Failing control, then fix: without the id on the wire the append lands and the join still reads
    ``projected_row_missing``; with it the join lands and the floor reads golden_only."""
    rig = _Rig([_reading_row(GOLDEN_AT, "WORKING")])
    wire = _WireProjection(rig.projected, tmp_path / "heard.txt")

    result = _wire_runner(rig, wire).handle(ModelJoinJ5RunCommand())

    assert [i.outcome for i in result.items] == [EnumJoinJ5Outcome.LANDED], result.items
    assert len(rig.ledger.append_calls) == 1
    assert _floor_read(rig, NOW + timedelta(minutes=5)) == ("golden_only", [])


def test_the_projection_read_carries_the_chain_id_not_a_fresh_one(
    tmp_path: Path,
) -> None:
    """The id on the wire is the join run's: the reading's own correlation id, in the reader's UUID form."""
    seen: list[str | None] = []

    class _Spy(InMemoryLedger):
        def read(self, query: ModelLedgerQuery) -> ModelLedgerRead:
            seen.append(query.correlation_id)
            return super().read(query)

    row = _reading_row(GOLDEN_AT, "WORKING")
    cid = "0123456789abcdef0123456789abcdef"
    SourceProjectedRows(
        _Spy([row]),
        attempts=1,
        interval_seconds=0.1,
        slack_seconds=60,
        clock=lambda: NOW,
    ).read_row(ledger_id=LEDGER_ID, ledger_seq=7, raw_row=row, correlation_id=cid)
    assert seen == [cid]


def test_the_wire_form_of_a_correlation_id_is_a_uuid_and_stays_traceable() -> None:
    from uuid import UUID

    from omnimarket.nodes.node_reading_writer_effect.adapters.ledger_adapters import (
        wire_correlation_id,
    )

    cid = "0123456789abcdef0123456789abcdef"
    assert wire_correlation_id(cid) == str(UUID(cid))
    # An id that is not a UUID is mapped to one by a fixed rule, the same every time.
    assert wire_correlation_id("not-a-uuid") == wire_correlation_id("not-a-uuid")
    assert str(UUID(wire_correlation_id("not-a-uuid"))) == wire_correlation_id(
        "not-a-uuid"
    )
    assert wire_correlation_id("not-a-uuid") != wire_correlation_id("another")
