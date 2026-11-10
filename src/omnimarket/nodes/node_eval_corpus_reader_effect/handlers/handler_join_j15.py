# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""J15 (piece 15 to piece 16): a delegated result and its cost land in the eval corpus (OMN-22978).

Canonical shape: ``handle(ModelJoinJ15Request) -> ModelJoinJ15Result``. The handler answers piece 16 on the floor's
emission contract under the correlation id piece 15's chain carries:

* a requested run piece 15 holds no result for: a typed ``upstream_`` failure that never started, nothing written;
* otherwise every result is written to the corpus with its cost row, or typed ``missing`` when none exists, and the
  corpus reader reads the runs back; the chain starts first when every run has its cost;
* a run the reader does not return, or returns as something other than the entry this join wrote (an older row left
  behind by a dropped append), is a typed ``entry_dropped`` failure (a silent drop made loud) that keeps its own code
  on the emission;
* a run with no cost row is a typed ``upstream_`` ``cost_missing`` failure with no start (piece 15's chain failed
  there), and its entry stays in the corpus typed missing;
* a success only when every run is read back with its cost.

A replayed request (a repeated delivery, a retry after a failed append) keeps one logical chain: an emission whose
phase the sink already holds under the correlation id is not written again.

Everything it calls is injected: the two sources, the corpus, the emission sink and the clock. Its settings are the
contract's ``join_j15`` section.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from importlib import resources
from typing import Any, Literal

import yaml

from omnimarket.handlers.emission_sink import ChainEmitter, ProtocolEmissionSink
from omnimarket.models.eval_corpus import (
    EnumCorpusCostStatus,
    EnumJoinJ15Outcome,
    ModelCorpusReadRequest,
    ModelEvalCorpusEntry,
    ModelJoinJ15Policy,
    ModelJoinJ15Request,
    ModelJoinJ15Result,
)
from omnimarket.models.floor_17a import ModelNodeEmission
from omnimarket.nodes.node_eval_corpus_reader_effect.handlers.handler_eval_corpus_reader import (
    HandlerEvalCorpusReader,
)
from omnimarket.nodes.node_eval_corpus_reader_effect.protocols import (
    ProtocolDelegatedResults,
    ProtocolDelegationCosts,
    ProtocolEvalCorpus,
)

_NODE = "node_eval_corpus_reader_effect"
_SECTION = "join_j15"


def load_join_j15_policy(
    contract: Mapping[str, Any] | None = None,
) -> ModelJoinJ15Policy:
    """The contract's ``join_j15`` section; an absent section or key refuses naming it."""
    if contract is None:
        contract = yaml.safe_load(
            resources.files(f"omnimarket.nodes.{_NODE}")
            .joinpath("contract.yaml")
            .read_text()
        )
    section = contract.get(_SECTION) if contract else None
    if not isinstance(section, dict):
        raise ValueError(f"{_NODE} contract declares no {_SECTION} section")
    for key in ModelJoinJ15Policy.model_fields:
        if key not in section:
            raise ValueError(f"{_NODE} contract {_SECTION}.{key} is absent")
    return ModelJoinJ15Policy(**section)


_TERMINAL_PHASES = ("success", "failure")


class _ReplaySafeSink:
    """Keeps one logical chain per correlation id across replays of the same request.

    A start is not written again when the sink already holds a start or a terminal for this chain, and a terminal is
    not written again when the sink already holds one, so repeated deliveries and partial-work retries add no
    ``duplicate_start`` or second terminal to the receipt fold.
    """

    def __init__(
        self,
        sink: ProtocolEmissionSink,
        *,
        node: str,
        piece: int,
        correlation_id: str,
    ) -> None:
        self._sink = sink
        self._node = node
        self._piece = piece
        self._correlation_id = correlation_id
        held = [
            e for e in sink.read(datetime.min.replace(tzinfo=UTC)) if self._is_mine(e)
        ]
        self._has_start = any(e.phase == "start" for e in held)
        self._has_terminal = any(e.phase in _TERMINAL_PHASES for e in held)

    def _is_mine(self, event: ModelNodeEmission) -> bool:
        return (
            event.node == self._node
            and event.piece == self._piece
            and event.correlation_id == self._correlation_id
        )

    def emit(self, event: ModelNodeEmission) -> None:
        if event.phase == "start":
            if self._has_start or self._has_terminal:
                return
            self._has_start = True
        elif event.phase in _TERMINAL_PHASES:
            if self._has_terminal:
                return
            self._has_terminal = True
        self._sink.emit(event)

    def read(self, since: datetime) -> tuple[ModelNodeEmission, ...]:
        return self._sink.read(since)


class HandlerJoinJ15:
    def __init__(
        self,
        *,
        results: ProtocolDelegatedResults,
        costs: ProtocolDelegationCosts,
        corpus: ProtocolEvalCorpus,
        sink: ProtocolEmissionSink,
        policy: ModelJoinJ15Policy,
        clock: Callable[[], datetime] | None = None,
        backend: Literal["in_memory", "real"] = "real",
    ) -> None:
        self._results = results
        self._costs = costs
        self._corpus = corpus
        self._reader = HandlerEvalCorpusReader(corpus=corpus)
        self._sink = sink
        self._policy = policy
        self._clock = clock or (lambda: datetime.now(UTC))
        self._backend = backend

    @property
    def handler_type(self) -> Literal["NODE_HANDLER"]:
        return "NODE_HANDLER"

    @property
    def handler_category(self) -> Literal["EFFECT"]:
        return "EFFECT"

    def handle(self, request: ModelJoinJ15Request) -> ModelJoinJ15Result:
        policy = self._policy
        chain = ChainEmitter(
            _ReplaySafeSink(
                self._sink,
                node=policy.emission_node,
                piece=policy.to_piece,
                correlation_id=request.correlation_id,
            ),
            node=policy.emission_node,
            piece=policy.to_piece,
            clock=self._clock,
            correlation_id=request.correlation_id,
            backend=self._backend,
        )
        results = self._results.read_results(request.run_ids)
        held = {r.run_id for r in results}
        no_result = tuple(i for i in request.run_ids if i not in held)
        if no_result:
            code = policy.no_result_failure_code
            detail = f"piece 15 holds no delegated result for {', '.join(no_result)}"
            chain.refuse(code, detail)
            return ModelJoinJ15Result(
                outcome=EnumJoinJ15Outcome.REFUSED_NO_DELEGATED_RESULT,
                correlation_id=request.correlation_id,
                failure_code=code,
                detail=detail,
                absent_run_ids=no_result,
            )

        costs = {r.run_id: self._costs.read_cost(r.run_id) for r in results}
        cost_less = tuple(i for i, c in costs.items() if c is None)
        # Piece 15's chain failed for a run with no cost row, so this chain never starts for it: the floor reads a
        # failure that never started as a refusal. The result is still written, typed missing: no silent drop.
        if not cost_less:
            chain.start()
        written: dict[str, ModelEvalCorpusEntry] = {}
        for result in results:
            cost = costs[result.run_id]
            entry = ModelEvalCorpusEntry(
                run_id=result.run_id,
                task_class=result.task_class,
                model_id=result.model_id,
                outcome=result.outcome,
                result_summary=result.result_summary,
                cost_status=EnumCorpusCostStatus.PRESENT
                if cost is not None
                else EnumCorpusCostStatus.MISSING,
                cost=cost,
                joined_at=self._clock(),
            )
            self._corpus.append(entry)
            written[entry.run_id] = entry

        read = self._reader.handle(ModelCorpusReadRequest(run_ids=request.run_ids))
        # An entry that reads back is the join's only when it equals what was written: a dropped replacement can
        # leave an older row for the run, which would otherwise read as landed.
        stale = {e.run_id for e in read.entries if e != written.get(e.run_id)}
        dropped = tuple(
            i for i in request.run_ids if i in read.absent_run_ids or i in stale
        )
        if dropped:
            code = policy.entry_dropped_failure_code
            detail = f"the corpus returned no current entry for {', '.join(dropped)}"
            if not chain.open:
                # Piece 15's chain failed for a cost-less run, so this chain never started. The drop keeps its own
                # code: it is not the upstream refusal the floor reads as the expected error case, so the chain
                # starts and fails under the drop code and the floor names it.
                chain.start()
            chain.failure(code, detail)
            return ModelJoinJ15Result(
                outcome=EnumJoinJ15Outcome.ENTRY_DROPPED,
                correlation_id=request.correlation_id,
                failure_code=code,
                detail=detail,
                entries=read.entries,
                missing_cost_run_ids=read.missing_cost_run_ids,
                absent_run_ids=dropped,
            )
        if read.missing_cost_run_ids:
            code = policy.cost_missing_failure_code
            detail = f"no cost row for {', '.join(read.missing_cost_run_ids)}; typed missing in the corpus"
            if chain.open:
                chain.failure(code, detail)
            else:
                chain.refuse(code, detail)
            return ModelJoinJ15Result(
                outcome=EnumJoinJ15Outcome.COST_MISSING,
                correlation_id=request.correlation_id,
                failure_code=code,
                detail=detail,
                entries=read.entries,
                missing_cost_run_ids=read.missing_cost_run_ids,
            )

        chain.success(
            {"results_joined": len(results), "entries_read": len(read.entries)}
        )
        return ModelJoinJ15Result(
            outcome=EnumJoinJ15Outcome.LANDED,
            correlation_id=request.correlation_id,
            entries=read.entries,
        )


__all__ = ["HandlerJoinJ15", "load_join_j15_policy"]
