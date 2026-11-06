# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The read-only principal's probe emits its golden and error chain through the floor's emission journal (OMN-22566).

Every test reads the chain back through the floor's own receipt fold (``HandlerFloorReceipt``), not through the sink,
so what is asserted is what the floor counts. Each error test has its control beside it: the same probe with the fault
removed must read golden, which shows the error assertions can fail.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

from omnimarket.handlers.emission_sink import (
    ChainEmitter,
    InMemoryEmissionSink,
    JsonlEmissionSink,
)
from omnimarket.handlers.node_overlay_reader import NodeOverlayError
from omnimarket.models.floor_17a import (
    ModelFloorReceiptInput,
    ModelFloorReceipts,
    ModelNodeEmission,
    ModelPieceDef,
)
from omnimarket.nodes.node_floor_emission_compute.handlers.handler_floor_receipt import (
    HandlerFloorReceipt,
)
from omnimarket.nodes.node_read_only_principal_effect.handlers.adapter_memory_attempt_executor import (
    MemoryAttemptExecutor,
)
from omnimarket.nodes.node_read_only_principal_effect.handlers.handler_read_only_principal_probe import (
    EMIT_NODE,
    FAILURE_CANNOT_RUN,
    FAILURE_INCONCLUSIVE,
    FAILURE_MUTATION_ACCEPTED,
    HandlerReadOnlyPrincipalProbe,
)
from omnimarket.nodes.node_read_only_principal_effect.models import (
    EnumChannel,
    ModelAllowedCommand,
    ModelChannelProbe,
    ModelProbeAttempt,
    ModelProbeEmission,
    ModelProbeRequest,
    ModelReadOnlyPrincipalOverlay,
    ModelSshGate,
)
from omnimarket.nodes.node_read_only_principal_effect.protocols.protocol_attempt_executor import (
    ModelAttemptRun,
)

pytestmark = pytest.mark.unit

PIECE = 3
NOW = datetime(2026, 11, 6, 12, 0, tzinfo=UTC)
JOURNAL = ".onex_state/floor-simple/emissions.jsonl"
REFUSED = ModelAttemptRun(exit_code=1, text="HTTP 403: Permission denied")
READ_OK = ModelAttemptRun(exit_code=0, text="fine")
ACCEPTED = ModelAttemptRun(exit_code=0, text="done")


def _clock() -> Callable[[], datetime]:
    state = {"n": 0}

    def tick() -> datetime:
        state["n"] += 1
        return NOW + timedelta(seconds=state["n"])

    return tick


def _overlay(emission: ModelProbeEmission | None) -> ModelReadOnlyPrincipalOverlay:
    probes = tuple(
        ModelChannelProbe(
            channel=channel,
            read=ModelProbeAttempt(argv=(f"{channel.value}-probe", "read")),
            mutate=ModelProbeAttempt(argv=(f"{channel.value}-probe", "mutate")),
            refusal_markers=("permission denied",),
        )
        for channel in EnumChannel
    )
    return ModelReadOnlyPrincipalOverlay(
        principal="ro-test",
        ssh=ModelSshGate(
            allowed_commands=(ModelAllowedCommand(argv_prefix=("uptime",)),),
            gate_command="/usr/local/bin/onex-read-only-principal gate",
            key_path="/keys/read_only_principal",
            key_comment="read-only-principal",
        ),
        probes=probes,
        emission=emission,
    )


def _emission() -> ModelProbeEmission:
    return ModelProbeEmission(piece=PIECE, journal_relpath=JOURNAL)


def _answers(
    read_fails: tuple[str, ...] = (), **mutations: ModelAttemptRun
) -> MemoryAttemptExecutor:
    """Every channel reads fine and refuses its mutation, except the channels named in ``mutations`` and ``read_fails``."""
    answers: dict[tuple[str, ...], ModelAttemptRun] = {}
    for channel in EnumChannel:
        stem = f"{channel.value}-probe"
        answers[(stem, "read")] = (
            ModelAttemptRun(exit_code=1, text="boom")
            if channel.value in read_fails
            else READ_OK
        )
        answers[(stem, "mutate")] = mutations.get(channel.value, REFUSED)
    return MemoryAttemptExecutor(answers)


def _probe(
    sink: InMemoryEmissionSink, executor: MemoryAttemptExecutor, emission: bool = True
) -> HandlerReadOnlyPrincipalProbe:
    overlay = _overlay(_emission() if emission else None)
    return HandlerReadOnlyPrincipalProbe(
        executor,
        lambda: overlay,
        sink=sink,
        clock=_clock(),
    )


def _fold(sink: InMemoryEmissionSink) -> ModelFloorReceipts:
    return HandlerFloorReceipt().handle(
        ModelFloorReceiptInput(
            now=NOW + timedelta(minutes=5),
            emissions=tuple(sink.events),
            pieces=(ModelPieceDef(piece=PIECE, name="probe", reading_max_age_s=900),),
            joins=(),
            freshness_max_age_s=900,
            error_max_age_s=900,
            chain_deadline_s=300,
        )
    )


class _Raises:
    """An executor that cannot run a program at all."""

    def run(self, attempt: ModelProbeAttempt) -> ModelAttemptRun:
        raise RuntimeError("the attempt could not be started")


# ------------------------------------------------------------------------------------------------------------ golden


def test_golden_a_probe_that_reads_and_is_refused_counts_one_golden_chain() -> None:
    sink = InMemoryEmissionSink()
    result = _probe(sink, _answers()).handle(ModelProbeRequest())

    assert result.verdict == "PASS"
    assert [e.phase for e in sink.events] == ["start", "success"]
    assert {e.correlation_id for e in sink.events} != set()
    assert len({e.correlation_id for e in sink.events}) == 1
    assert {e.node for e in sink.events} == {EMIT_NODE}
    assert {e.piece for e in sink.events} == {PIECE}
    assert sink.events[1].counters == {
        "channels": 3,
        "reads_ok": 3,
        "mutations_refused": 3,
    }

    receipts = _fold(sink)
    assert receipts.findings == ()
    (chain,) = receipts.chains
    assert chain.kind == "golden"
    assert chain.piece == PIECE
    (row,) = receipts.pieces
    assert row.golden_at is not None
    assert row.error_at is None
    assert row.golden_backends == ("real",)


def test_golden_each_run_is_its_own_chain_under_its_own_correlation_id() -> None:
    sink = InMemoryEmissionSink()
    probe = _probe(sink, _answers())
    probe.handle(ModelProbeRequest())
    probe.handle(ModelProbeRequest())

    receipts = _fold(sink)
    assert [c.kind for c in receipts.chains] == ["golden", "golden"]
    assert len({c.correlation_id for c in receipts.chains}) == 2


def test_golden_a_narrowed_probe_that_is_refused_is_still_a_success_with_its_channels() -> (
    None
):
    sink = InMemoryEmissionSink()
    result = _probe(sink, _answers()).handle(
        ModelProbeRequest(channels=(EnumChannel.GITHUB,))
    )

    assert result.verdict == "PASS-PARTIAL"
    assert [e.phase for e in sink.events] == ["start", "success"]
    assert sink.events[1].counters["channels"] == 1


def test_golden_the_chain_lands_in_the_floors_journal_file(tmp_path: Path) -> None:
    overlay = _overlay(_emission())
    handler = HandlerReadOnlyPrincipalProbe(
        _answers(),
        lambda: overlay,
        clock=_clock(),
        environ={"OMNI_HOME": str(tmp_path)},
    )
    handler.handle(ModelProbeRequest())

    journal = JsonlEmissionSink(tmp_path / JOURNAL)
    events = journal.read(NOW)
    assert [e.phase for e in events] == ["start", "success"]
    assert journal.skipped_lines == 0
    # The floor's own cycle appends to the same file: both chains are read by one fold.
    ChainEmitter(journal, node="node_floor_simple_effect:run", piece=9).start()
    assert {e.piece for e in journal.read(NOW)} == {PIECE, 9}


def test_golden_an_overlay_without_emission_emits_nothing() -> None:
    sink = InMemoryEmissionSink()
    result = _probe(sink, _answers(), emission=False).handle(ModelProbeRequest())

    assert result.verdict == "PASS"
    assert sink.events == []


# ------------------------------------------------------------------------------------------------------------- error


def test_error_a_mutation_that_goes_through_is_a_typed_failure_naming_it() -> None:
    sink = InMemoryEmissionSink()
    result = _probe(sink, _answers(github=ACCEPTED)).handle(ModelProbeRequest())

    assert result.verdict == "FAIL"
    assert [e.phase for e in sink.events] == ["start", "failure"]
    failure = sink.events[1]
    assert failure.failure_code == FAILURE_MUTATION_ACCEPTED
    assert "github" in failure.failure_detail
    assert "ssh" not in failure.failure_detail
    assert len({e.correlation_id for e in sink.events}) == 1

    receipts = _fold(sink)
    assert receipts.findings == ()
    (chain,) = receipts.chains
    assert chain.kind == "error"
    (row,) = receipts.pieces
    assert row.error_at is not None
    assert row.golden_at is None


def test_error_the_failure_names_every_channel_that_accepted() -> None:
    sink = InMemoryEmissionSink()
    _probe(sink, _answers(ssh=ACCEPTED, ledger=ACCEPTED)).handle(ModelProbeRequest())

    detail = sink.events[-1].failure_detail
    assert "ssh" in detail
    assert "ledger" in detail
    assert "github" not in detail


def test_error_control_the_same_probe_without_the_fault_reads_golden() -> None:
    faulty, clean = InMemoryEmissionSink(), InMemoryEmissionSink()
    _probe(faulty, _answers(ssh=ACCEPTED)).handle(ModelProbeRequest())
    _probe(clean, _answers()).handle(ModelProbeRequest())

    assert [c.kind for c in _fold(faulty).chains] == ["error"]
    assert [c.kind for c in _fold(clean).chains] == ["golden"]


def test_error_a_read_that_fails_is_an_inconclusive_failure_and_not_a_golden_chain() -> (
    None
):
    sink = InMemoryEmissionSink()
    _probe(sink, _answers(read_fails=("ledger",))).handle(ModelProbeRequest())

    failure = sink.events[-1]
    assert failure.phase == "failure"
    assert failure.failure_code == FAILURE_INCONCLUSIVE
    assert "ledger" in failure.failure_detail
    assert [c.kind for c in _fold(sink).chains] == ["error"]


def test_error_a_mutation_that_fails_without_a_refusal_word_is_inconclusive() -> None:
    sink = InMemoryEmissionSink()
    _probe(
        sink, _answers(ssh=ModelAttemptRun(exit_code=255, text="connection timed out"))
    ).handle(ModelProbeRequest())

    assert sink.events[-1].failure_code == FAILURE_INCONCLUSIVE
    assert "mutation-indeterminate" in sink.events[-1].failure_detail


def test_error_a_golden_run_and_a_fault_run_start_the_piece() -> None:
    sink = InMemoryEmissionSink()
    _probe(sink, _answers()).handle(ModelProbeRequest())
    _probe(sink, _answers(ssh=ACCEPTED)).handle(ModelProbeRequest())

    receipts = _fold(sink)
    assert sorted(c.kind for c in receipts.chains) == ["error", "golden"]
    (row,) = receipts.pieces
    assert row.started is True
    assert row.freshness == "fresh"


def test_error_a_failure_never_carries_the_attempt_text() -> None:
    sink = InMemoryEmissionSink()
    secret = ModelAttemptRun(exit_code=0, text="token ghp_SECRETVALUE")
    _probe(sink, _answers(github=secret)).handle(ModelProbeRequest())

    dumped = " ".join(e.model_dump_json() for e in sink.events)
    assert "ghp_SECRETVALUE" not in dumped


# ------------------------------------------------------------------------------------------------------- cannot run


def test_cannot_run_an_executor_that_raises_is_a_typed_failure_and_the_error_surfaces() -> (
    None
):
    sink = InMemoryEmissionSink()
    overlay = _overlay(_emission())
    handler = HandlerReadOnlyPrincipalProbe(
        _Raises(),  # type: ignore[arg-type]
        lambda: overlay,
        sink=sink,
        clock=_clock(),
    )
    with pytest.raises(RuntimeError, match="could not be started"):
        handler.handle(ModelProbeRequest())

    assert [e.phase for e in sink.events] == ["start", "failure"]
    failure = sink.events[1]
    assert failure.failure_code == FAILURE_CANNOT_RUN
    assert failure.failure_detail == "RuntimeError"
    receipts = _fold(sink)
    assert receipts.findings == ()
    assert [c.kind for c in receipts.chains] == ["error"]
    assert receipts.pieces[0].golden_at is None


def test_cannot_run_a_missing_journal_home_refuses_before_any_start(
    tmp_path: Path,
) -> None:
    overlay = _overlay(_emission())
    executor = _answers()
    handler = HandlerReadOnlyPrincipalProbe(executor, lambda: overlay, environ={})
    with pytest.raises(NodeOverlayError, match="OMNI_HOME is not set"):
        handler.handle(ModelProbeRequest())

    assert executor.calls == []
    assert not (tmp_path / JOURNAL).exists()


def test_cannot_run_an_interrupt_closes_the_start() -> None:
    class _Interrupted:
        def run(self, attempt: ModelProbeAttempt) -> ModelAttemptRun:
            raise KeyboardInterrupt

    sink = InMemoryEmissionSink()
    overlay = _overlay(_emission())
    handler = HandlerReadOnlyPrincipalProbe(
        _Interrupted(),  # type: ignore[arg-type]
        lambda: overlay,
        sink=sink,
        clock=_clock(),
    )
    with pytest.raises(KeyboardInterrupt):
        handler.handle(ModelProbeRequest())

    assert [e.phase for e in sink.events] == ["start", "failure"]
    assert sink.events[1].failure_code == "interrupted"


# ------------------------------------------------------------------------------------------- the contract and overlay


def test_error_the_chain_emitter_refuses_to_break_the_emission_contract() -> None:
    sink = InMemoryEmissionSink()
    chain = ChainEmitter(sink, node="n", piece=1)
    with pytest.raises(RuntimeError, match="only after its start"):
        chain.success()
    chain.start()
    with pytest.raises(RuntimeError, match="starts once"):
        chain.start()
    chain.success({"n": 1})
    with pytest.raises(RuntimeError, match="ends once"):
        chain.failure("late")
    assert [e.phase for e in sink.events] == ["start", "success"]
    assert all(isinstance(e, ModelNodeEmission) for e in sink.events)


@pytest.mark.parametrize(
    "relpath", ["/abs/emissions.jsonl", "../escape.jsonl", "a/../b"]
)
def test_error_a_journal_path_that_leaves_the_home_is_refused(relpath: str) -> None:
    with pytest.raises(ValidationError, match="stays inside the home"):
        ModelProbeEmission(piece=PIECE, journal_relpath=relpath)


@pytest.mark.parametrize("piece", [0, 19])
def test_error_a_piece_outside_the_eighteen_is_refused(piece: int) -> None:
    with pytest.raises(ValidationError):
        ModelProbeEmission(piece=piece, journal_relpath=JOURNAL)
