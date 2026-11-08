# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""J15 (piece 15 to piece 16): a delegated result and its cost land in the eval corpus (OMN-22978).

Canonical shape: ``handle(ModelJoinJ15Request) -> ModelJoinJ15Result``. The handler answers piece 16 on the floor's
emission contract under the correlation id piece 15's chain carries:

* a requested run piece 15 holds no result for: a typed ``upstream_`` failure that never started, nothing written;
* otherwise every result is written to the corpus with its cost row, or typed ``missing`` when none exists, and the
  corpus reader reads the runs back; the chain starts first when every run has its cost;
* a run the reader does not return is a typed ``entry_dropped`` failure (a silent drop made loud);
* a run with no cost row is a typed ``upstream_`` ``cost_missing`` failure with no start (piece 15's chain failed
  there), and its entry stays in the corpus typed missing;
* a success only when every run is read back with its cost.

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
            self._sink,
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
        for result in results:
            cost = costs[result.run_id]
            self._corpus.append(
                ModelEvalCorpusEntry(
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
            )

        read = self._reader.handle(ModelCorpusReadRequest(run_ids=request.run_ids))
        if read.absent_run_ids:
            code = policy.entry_dropped_failure_code
            detail = (
                f"the corpus returned no entry for {', '.join(read.absent_run_ids)}"
            )
            if chain.open:
                chain.failure(code, detail)
            else:
                # Piece 15's chain failed for a cost-less run, so this chain never started: the drop is named in
                # the detail and the refusal keeps the upstream code the floor reads.
                chain.refuse(policy.cost_missing_failure_code, detail)
            return ModelJoinJ15Result(
                outcome=EnumJoinJ15Outcome.ENTRY_DROPPED,
                correlation_id=request.correlation_id,
                failure_code=code,
                detail=detail,
                entries=read.entries,
                missing_cost_run_ids=read.missing_cost_run_ids,
                absent_run_ids=read.absent_run_ids,
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
