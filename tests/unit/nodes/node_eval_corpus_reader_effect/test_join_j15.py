# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""J15 (piece 15 to piece 16): a delegated result and its cost land in the eval corpus (OMN-22978).

Rebuild plan 4.1, J15: "A delegated result and its cost land in the eval corpus", through the corpus reader.

The join is judged the way the floor judges it: piece 15's chain and piece 16's chain share one correlation id and the
floor's own receipt fold reads the pair. The golden case reads ``golden_only``, the error case ``error_only``, both
together ``passed``. Every run here is in memory or in a file under ``tmp_path``.

Two injected breaks show the tests can fail for real: a corpus that drops the entries that have no cost (the silent
drop the error case forbids) and a cost source that invents a zero cost row for a run with none (the typed ``missing``
the error case demands would then read ``present``). ``test_control_*`` pin both.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest
import yaml

from omnimarket.handlers.emission_sink import ChainEmitter, InMemoryEmissionSink
from omnimarket.handlers.node_overlay_reader import NodeOverlayError
from omnimarket.models.eval_corpus import (
    EnumCorpusCostStatus,
    EnumJoinJ15Outcome,
    ModelCorpusReadRequest,
    ModelDelegatedResult,
    ModelDelegationCost,
    ModelEvalCorpusEntry,
    ModelJoinJ15Policy,
    ModelJoinJ15Request,
)
from omnimarket.models.floor_17a import (
    ModelFloorReceiptInput,
    ModelJoinDef,
    ModelNodeEmission,
    ModelPieceDef,
)
from omnimarket.nodes.node_eval_corpus_reader_effect.handlers.handler_eval_corpus_reader import (
    HandlerEvalCorpusReader,
)
from omnimarket.nodes.node_eval_corpus_reader_effect.handlers.handler_join_j15 import (
    HandlerJoinJ15,
    load_join_j15_policy,
)
from omnimarket.nodes.node_eval_corpus_reader_effect.overlay import (
    ModelEvalCorpusOverlay,
    build_corpus,
    load_overlay,
)
from omnimarket.nodes.node_eval_corpus_reader_effect.protocols import (
    InMemoryDelegatedResults,
    InMemoryDelegationCosts,
    InMemoryEvalCorpus,
    JsonlEvalCorpus,
)
from omnimarket.nodes.node_floor_emission_compute.handlers.handler_floor_receipt import (
    HandlerFloorReceipt,
)

pytestmark = pytest.mark.unit

NOW = datetime(2026, 11, 7, 13, 0, 0, tzinfo=UTC)
POLICY = load_join_j15_policy()
CHAIN_DEADLINE_S = 900


class _Clock:
    """Moves one second per read, so a chain's events keep their order."""

    def __init__(self) -> None:
        self._t = NOW

    def __call__(self) -> datetime:
        self._t += timedelta(seconds=1)
        return self._t


def _result(run_id: str) -> ModelDelegatedResult:
    return ModelDelegatedResult(
        run_id=run_id,
        task_class="document",
        model_id="lab-qwen",
        outcome="completed",
        result_summary=f"summary of {run_id}",
    )


def _cost(run_id: str, usd: str = "0.0042") -> ModelDelegationCost:
    return ModelDelegationCost(
        run_id=run_id,
        cost_usd=Decimal(usd),
        input_tokens=1200,
        output_tokens=340,
        measurement_source="receipt",
    )


class _DroppingCorpus(InMemoryEvalCorpus):
    """Injected break: silently drops every entry that has no cost row."""

    def append(self, entry: ModelEvalCorpusEntry) -> None:
        if entry.cost_status is EnumCorpusCostStatus.MISSING:
            return
        super().append(entry)


class _InventingCosts(InMemoryDelegationCosts):
    """Injected break: answers a zero cost row for a run that has none."""

    def read_cost(self, run_id: str) -> ModelDelegationCost | None:
        return super().read_cost(run_id) or _cost(run_id, "0")


class _Rig:
    def __init__(
        self,
        *,
        results: list[ModelDelegatedResult],
        costs: list[ModelDelegationCost],
        corpus: InMemoryEvalCorpus | JsonlEvalCorpus | None = None,
        costs_source: InMemoryDelegationCosts | None = None,
    ) -> None:
        self.corpus = corpus if corpus is not None else InMemoryEvalCorpus()
        self.sink = InMemoryEmissionSink()
        self.clock = _Clock()
        self.join = HandlerJoinJ15(
            results=InMemoryDelegatedResults(results),
            costs=costs_source or InMemoryDelegationCosts(costs),
            corpus=self.corpus,
            sink=self.sink,
            policy=POLICY,
            clock=self.clock,
            backend="in_memory",
        )

    def upstream_chain(self, cid: str, *, cost_present: bool) -> None:
        """Piece 15 as it answers the same runs: a start, then a success, or a typed failure when a cost is missing."""
        chain = ChainEmitter(
            self.sink,
            node="node_delegation_orchestrator",
            piece=POLICY.from_piece,
            clock=self.clock,
            correlation_id=cid,
            backend="in_memory",
        )
        chain.start()
        if cost_present:
            chain.success({"results": 1, "costs": 1})
        else:
            chain.failure(
                "delegation_cost_missing", "a delegated result with no cost row"
            )


def _fold(emissions: list[ModelNodeEmission]) -> tuple[str, list[str]]:
    receipts = HandlerFloorReceipt().handle(
        ModelFloorReceiptInput(
            now=NOW + timedelta(seconds=60),
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
                    label="delegation to context and evals",
                ),
            ),
            freshness_max_age_s=86400,
            error_max_age_s=86400,
            chain_deadline_s=CHAIN_DEADLINE_S,
        )
    )
    join = next(j for j in receipts.joins if j.join == POLICY.join_id)
    return join.state, [f.code for f in receipts.findings]


# --- the golden join ----------------------------------------------------------------------------------------------


def test_join_j15_golden_result_and_cost_appear_in_the_corpus_the_reader_reads() -> (
    None
):
    rig = _Rig(results=[_result("run-1")], costs=[_cost("run-1")])
    cid = uuid4().hex
    rig.upstream_chain(cid, cost_present=True)

    result = rig.join.handle(
        ModelJoinJ15Request(correlation_id=cid, run_ids=("run-1",))
    )

    assert result.outcome is EnumJoinJ15Outcome.LANDED
    assert result.failure_code is None
    # The corpus reader, a separate handler over the same corpus, returns the result with its cost.
    read = HandlerEvalCorpusReader(corpus=rig.corpus).handle(
        ModelCorpusReadRequest(run_ids=("run-1",))
    )
    assert read.missing_cost_run_ids == ()
    assert read.absent_run_ids == ()
    (entry,) = read.entries
    assert entry.cost_status is EnumCorpusCostStatus.PRESENT
    assert entry.result_summary == "summary of run-1"
    assert entry.outcome == "completed"
    assert entry.cost is not None
    assert entry.cost.cost_usd == Decimal("0.0042")
    assert (entry.cost.input_tokens, entry.cost.output_tokens) == (1200, 340)
    assert result.entries == read.entries


def test_join_j15_golden_chain_event_piece_16_follows_piece_15_and_the_floor_reads_golden() -> (
    None
):
    rig = _Rig(
        results=[_result("run-1"), _result("run-2")],
        costs=[_cost("run-1"), _cost("run-2")],
    )
    cid = uuid4().hex
    rig.upstream_chain(cid, cost_present=True)
    rig.join.handle(ModelJoinJ15Request(correlation_id=cid, run_ids=("run-1", "run-2")))

    mine = [e for e in rig.sink.events if e.piece == POLICY.to_piece]
    assert [e.phase for e in mine] == ["start", "success"]
    assert {e.correlation_id for e in mine} == {cid}
    assert mine[-1].counters == {"results_joined": 2, "entries_read": 2}
    assert _fold(rig.sink.events) == ("golden_only", [])


def test_join_j15_golden_on_the_journal_backend_the_overlay_selects(
    tmp_path: Path,
) -> None:
    overlay = ModelEvalCorpusOverlay(
        backend="jsonl",
        state_home_env="J15_TEST_STATE",
        corpus_journal_relpath="eval_corpus.jsonl",
        emission_journal_relpath="emissions.jsonl",
    )
    corpus = build_corpus(overlay, {"J15_TEST_STATE": str(tmp_path)})
    assert isinstance(corpus, JsonlEvalCorpus)
    rig = _Rig(results=[_result("run-1")], costs=[_cost("run-1")], corpus=corpus)
    rig.join.handle(ModelJoinJ15Request(correlation_id=uuid4().hex, run_ids=("run-1",)))

    # A fresh reader over the same file, as a later process would open it.
    fresh = HandlerEvalCorpusReader(
        corpus=JsonlEvalCorpus(tmp_path / "eval_corpus.jsonl")
    )
    (entry,) = fresh.handle(ModelCorpusReadRequest(run_ids=("run-1",))).entries
    assert entry.cost is not None
    assert entry.cost.cost_usd == Decimal("0.0042")


def test_join_j15_golden_default_overlay_backend_is_in_memory() -> None:
    overlay = ModelEvalCorpusOverlay.model_validate(
        yaml.safe_load(
            (
                Path(__file__).parents[4]
                / "src/omnimarket/nodes/node_eval_corpus_reader_effect/overlay.example.yaml"
            ).read_text()
        )
    )
    assert isinstance(build_corpus(overlay, {}), InMemoryEvalCorpus)
    # No overlay and no default: the loader refuses rather than choosing a backend.
    with pytest.raises(NodeOverlayError):
        load_overlay({})


# --- the error join -----------------------------------------------------------------------------------------------


def test_join_j15_error_result_with_no_cost_row_is_typed_missing_and_not_dropped() -> (
    None
):
    rig = _Rig(results=[_result("run-1"), _result("run-2")], costs=[_cost("run-1")])
    cid = uuid4().hex
    rig.upstream_chain(cid, cost_present=False)

    result = rig.join.handle(
        ModelJoinJ15Request(correlation_id=cid, run_ids=("run-1", "run-2"))
    )

    assert result.outcome is EnumJoinJ15Outcome.COST_MISSING
    assert result.failure_code == POLICY.cost_missing_failure_code
    assert result.missing_cost_run_ids == ("run-2",)
    read = HandlerEvalCorpusReader(corpus=rig.corpus).handle(
        ModelCorpusReadRequest(run_ids=("run-1", "run-2"))
    )
    # Both runs are in the corpus: nothing dropped. The one without a cost row is typed missing, with no cost.
    assert read.absent_run_ids == ()
    assert read.missing_cost_run_ids == ("run-2",)
    by_run = {e.run_id: e for e in read.entries}
    assert by_run["run-1"].cost_status is EnumCorpusCostStatus.PRESENT
    assert by_run["run-2"].cost_status is EnumCorpusCostStatus.MISSING
    assert by_run["run-2"].cost is None
    assert by_run["run-2"].result_summary == "summary of run-2"


def test_join_j15_error_chain_event_typed_failure_with_no_start_and_the_floor_reads_error() -> (
    None
):
    rig = _Rig(results=[_result("run-1")], costs=[])
    cid = uuid4().hex
    rig.upstream_chain(cid, cost_present=False)
    rig.join.handle(ModelJoinJ15Request(correlation_id=cid, run_ids=("run-1",)))

    mine = [e for e in rig.sink.events if e.piece == POLICY.to_piece]
    assert [e.phase for e in mine] == ["failure"]
    assert mine[0].failure_code == POLICY.cost_missing_failure_code
    assert mine[0].correlation_id == cid
    assert _fold(rig.sink.events) == ("error_only", [])


def test_join_j15_error_and_golden_together_read_passed_on_the_floor() -> None:
    rig = _Rig(results=[_result("run-1"), _result("run-2")], costs=[_cost("run-1")])
    good, bad = uuid4().hex, uuid4().hex
    rig.upstream_chain(good, cost_present=True)
    rig.join.handle(ModelJoinJ15Request(correlation_id=good, run_ids=("run-1",)))
    rig.upstream_chain(bad, cost_present=False)
    rig.join.handle(ModelJoinJ15Request(correlation_id=bad, run_ids=("run-2",)))

    assert _fold(rig.sink.events) == ("passed", [])


def test_join_j15_error_no_delegated_result_is_refused_typed_and_writes_no_entry() -> (
    None
):
    rig = _Rig(results=[], costs=[_cost("run-9")])
    cid = uuid4().hex
    result = rig.join.handle(
        ModelJoinJ15Request(correlation_id=cid, run_ids=("run-9",))
    )

    assert result.outcome is EnumJoinJ15Outcome.REFUSED_NO_DELEGATED_RESULT
    assert result.failure_code == POLICY.no_result_failure_code
    assert result.absent_run_ids == ("run-9",)
    entries, skipped = rig.corpus.read(())
    assert entries == ()
    assert skipped == 0
    assert [e.phase for e in rig.sink.events] == ["failure"]


def test_join_j15_error_a_corpus_that_drops_an_entry_is_named_not_swallowed() -> None:
    rig = _Rig(
        results=[_result("run-1"), _result("run-2")],
        costs=[_cost("run-1")],
        corpus=_DroppingCorpus(),
    )
    result = rig.join.handle(
        ModelJoinJ15Request(correlation_id=uuid4().hex, run_ids=("run-1", "run-2"))
    )

    assert result.outcome is EnumJoinJ15Outcome.ENTRY_DROPPED
    assert result.failure_code == POLICY.entry_dropped_failure_code
    assert result.absent_run_ids == ("run-2",)


def test_join_j15_error_an_unreadable_journal_line_is_counted_not_repaired(
    tmp_path: Path,
) -> None:
    journal = tmp_path / "eval_corpus.jsonl"
    corpus = JsonlEvalCorpus(journal)
    rig = _Rig(results=[_result("run-1")], costs=[_cost("run-1")], corpus=corpus)
    rig.join.handle(ModelJoinJ15Request(correlation_id=uuid4().hex, run_ids=("run-1",)))
    with journal.open("a", encoding="utf-8") as handle:
        handle.write("{not json\n")

    read = HandlerEvalCorpusReader(corpus=JsonlEvalCorpus(journal)).handle(
        ModelCorpusReadRequest()
    )
    assert read.skipped_lines == 1
    assert [e.run_id for e in read.entries] == ["run-1"]


def test_join_j15_error_a_cost_row_of_another_run_is_refused_by_the_entry() -> None:
    with pytest.raises(ValueError, match="another run"):
        ModelEvalCorpusEntry(
            run_id="run-1",
            task_class="document",
            model_id="lab-qwen",
            outcome="completed",
            result_summary="s",
            cost_status=EnumCorpusCostStatus.PRESENT,
            cost=_cost("run-2"),
            joined_at=NOW,
        )


# --- controls: the injected breaks, shown red ---------------------------------------------------------------------


def test_control_injected_break_inventing_a_cost_row_reads_present_where_the_error_case_needs_missing() -> (
    None
):
    rig = _Rig(
        results=[_result("run-2")],
        costs=[],
        costs_source=_InventingCosts(),
    )
    result = rig.join.handle(
        ModelJoinJ15Request(correlation_id=uuid4().hex, run_ids=("run-2",))
    )

    # With the break the join lands: the very reading the error case asserts is not produced.
    assert result.outcome is EnumJoinJ15Outcome.LANDED
    assert result.missing_cost_run_ids == ()
    assert result.outcome is not EnumJoinJ15Outcome.COST_MISSING


def test_control_injected_break_dropping_corpus_would_leave_run_2_unread() -> None:
    rig = _Rig(results=[_result("run-2")], costs=[], corpus=_DroppingCorpus())
    rig.join.handle(ModelJoinJ15Request(correlation_id=uuid4().hex, run_ids=("run-2",)))
    read = HandlerEvalCorpusReader(corpus=rig.corpus).handle(
        ModelCorpusReadRequest(run_ids=("run-2",))
    )
    assert read.absent_run_ids == ("run-2",)
    assert read.entries == ()


def test_control_policy_lives_in_the_contract_and_keeps_the_upstream_prefix() -> None:
    assert POLICY.join_id == "J15"
    assert (POLICY.from_piece, POLICY.to_piece) == (15, 16)
    with pytest.raises(ValueError, match="upstream_"):
        ModelJoinJ15Policy(
            **{**POLICY.model_dump(), "cost_missing_failure_code": "cost_missing"}
        )
    contract = yaml.safe_load(
        (
            Path(__file__).parents[4]
            / "src/omnimarket/nodes/node_eval_corpus_reader_effect/contract.yaml"
        ).read_text()
    )
    del contract["join_j15"]["entry_dropped_failure_code"]
    with pytest.raises(
        ValueError, match=r"join_j15\.entry_dropped_failure_code is absent"
    ):
        load_join_j15_policy(contract)


# --- review findings (omnimarket#5298): read-back, journal damage, replay, drop identity --------------------------


class _StaleReplacementCorpus(InMemoryEvalCorpus):
    """Injected break: holds an older row for a run and drops every later append, so the old row stays visible."""

    def __init__(self, stale: ModelEvalCorpusEntry) -> None:
        super().__init__()
        super().append(stale)

    def append(self, entry: ModelEvalCorpusEntry) -> None:
        return


def _stale_entry(run_id: str) -> ModelEvalCorpusEntry:
    return ModelEvalCorpusEntry(
        run_id=run_id,
        task_class="document",
        model_id="lab-qwen",
        outcome="completed",
        result_summary="an older summary",
        cost_status=EnumCorpusCostStatus.PRESENT,
        cost=_cost(run_id, "0.0001"),
        joined_at=NOW - timedelta(days=1),
    )


def test_join_j15_error_a_dropped_replacement_behind_an_older_cost_row_is_not_landed() -> (
    None
):
    rig = _Rig(
        results=[_result("run-1")],
        costs=[_cost("run-1")],
        corpus=_StaleReplacementCorpus(_stale_entry("run-1")),
    )
    result = rig.join.handle(
        ModelJoinJ15Request(correlation_id=uuid4().hex, run_ids=("run-1",))
    )

    assert result.outcome is EnumJoinJ15Outcome.ENTRY_DROPPED
    assert result.failure_code == POLICY.entry_dropped_failure_code
    assert result.absent_run_ids == ("run-1",)


def test_join_j15_error_a_dropped_cost_less_replacement_behind_an_older_cost_row_is_typed_not_a_crash() -> (
    None
):
    rig = _Rig(
        results=[_result("run-1")],
        costs=[],
        corpus=_StaleReplacementCorpus(_stale_entry("run-1")),
    )
    result = rig.join.handle(
        ModelJoinJ15Request(correlation_id=uuid4().hex, run_ids=("run-1",))
    )

    assert result.outcome is EnumJoinJ15Outcome.ENTRY_DROPPED
    assert result.absent_run_ids == ("run-1",)


def test_join_j15_error_a_torn_journal_tail_does_not_consume_the_next_append(
    tmp_path: Path,
) -> None:
    journal = tmp_path / "eval_corpus.jsonl"
    journal.write_text('{"run_id": "run-0", "task_cl', encoding="utf-8")
    corpus = JsonlEvalCorpus(journal)
    corpus.append(_stale_entry("run-1"))

    entries, skipped = JsonlEvalCorpus(journal).read(())
    assert [e.run_id for e in entries] == ["run-1"]
    assert skipped == 1


def test_join_j15_error_an_unreadable_journal_is_not_an_absent_one(
    tmp_path: Path,
) -> None:
    # An absent journal is an empty corpus; a directory at the journal path is unreadable storage, not "no entries".
    assert JsonlEvalCorpus(tmp_path / "absent.jsonl").read(()) == ((), 0)
    with pytest.raises(IsADirectoryError):
        JsonlEvalCorpus(tmp_path).read(())


def test_join_j15_error_an_undecodable_journal_record_does_not_block_the_valid_ones(
    tmp_path: Path,
) -> None:
    journal = tmp_path / "eval_corpus.jsonl"
    corpus = JsonlEvalCorpus(journal)
    corpus.append(_stale_entry("run-1"))
    with journal.open("ab") as handle:
        handle.write(b'{"run_id": "run-x", "result_summary": "\xe2\x82"}\n')
    corpus.append(_stale_entry("run-2"))

    entries, skipped = JsonlEvalCorpus(journal).read(())
    assert [e.run_id for e in entries] == ["run-1", "run-2"]
    assert skipped == 1


def test_join_j15_golden_a_replayed_request_keeps_one_valid_chain() -> None:
    rig = _Rig(results=[_result("run-1")], costs=[_cost("run-1")])
    cid = uuid4().hex
    rig.upstream_chain(cid, cost_present=True)
    request = ModelJoinJ15Request(correlation_id=cid, run_ids=("run-1",))
    first = rig.join.handle(request)
    second = rig.join.handle(request)

    assert first.outcome is second.outcome is EnumJoinJ15Outcome.LANDED
    mine = [e for e in rig.sink.events if e.piece == POLICY.to_piece]
    assert [e.phase for e in mine] == ["start", "success"]
    assert _fold(rig.sink.events) == ("golden_only", [])


class _FailsOnce(InMemoryEvalCorpus):
    """Injected break: the first append raises, after the chain has started."""

    def __init__(self) -> None:
        super().__init__()
        self.failed = False

    def append(self, entry: ModelEvalCorpusEntry) -> None:
        if not self.failed:
            self.failed = True
            raise OSError("disk full")
        super().append(entry)


def test_join_j15_golden_a_retry_after_an_append_failure_keeps_one_start() -> None:
    rig = _Rig(results=[_result("run-1")], costs=[_cost("run-1")], corpus=_FailsOnce())
    cid = uuid4().hex
    rig.upstream_chain(cid, cost_present=True)
    request = ModelJoinJ15Request(correlation_id=cid, run_ids=("run-1",))
    with pytest.raises(OSError, match="disk full"):
        rig.join.handle(request)
    result = rig.join.handle(request)

    assert result.outcome is EnumJoinJ15Outcome.LANDED
    mine = [e for e in rig.sink.events if e.piece == POLICY.to_piece]
    assert [e.phase for e in mine] == ["start", "success"]
    assert _fold(rig.sink.events) == ("golden_only", [])


def test_join_j15_error_a_corpus_drop_keeps_its_own_failure_code_on_the_emission() -> (
    None
):
    rig = _Rig(
        results=[_result("run-1"), _result("run-2")],
        costs=[_cost("run-1")],
        corpus=_DroppingCorpus(),
    )
    cid = uuid4().hex
    rig.upstream_chain(cid, cost_present=False)
    rig.join.handle(ModelJoinJ15Request(correlation_id=cid, run_ids=("run-1", "run-2")))

    mine = [e for e in rig.sink.events if e.piece == POLICY.to_piece]
    assert mine[-1].phase == "failure"
    assert mine[-1].failure_code == POLICY.entry_dropped_failure_code
    # The drop is not the expected upstream-cost refusal: the floor must not read it as the error case handled.
    state, _ = _fold(rig.sink.events)
    assert state not in ("error_only", "passed")
