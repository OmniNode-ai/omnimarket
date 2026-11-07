# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The corpus reader (OMN-22978): ``handle(ModelCorpusReadRequest) -> ModelCorpusReadResult``.

Reads the entries of the eval corpus from the injected corpus port. A result whose cost row does not exist reads
back typed ``missing`` and is listed in ``missing_cost_run_ids``; a requested run with no entry is listed in
``absent_run_ids``. Nothing is filtered out: a reader that dropped a row would hide the gap it exists to show.
"""

from __future__ import annotations

from typing import Literal

from omnimarket.models.eval_corpus import (
    EnumCorpusCostStatus,
    ModelCorpusReadRequest,
    ModelCorpusReadResult,
)
from omnimarket.nodes.node_eval_corpus_reader_effect.protocols import ProtocolEvalCorpus


class HandlerEvalCorpusReader:
    def __init__(self, *, corpus: ProtocolEvalCorpus) -> None:
        self._corpus = corpus

    @property
    def handler_type(self) -> Literal["NODE_HANDLER"]:
        return "NODE_HANDLER"

    @property
    def handler_category(self) -> Literal["EFFECT"]:
        return "EFFECT"

    def handle(self, request: ModelCorpusReadRequest) -> ModelCorpusReadResult:
        entries, skipped = self._corpus.read(request.run_ids)
        present = {e.run_id for e in entries}
        return ModelCorpusReadResult(
            entries=entries,
            missing_cost_run_ids=tuple(
                e.run_id
                for e in entries
                if e.cost_status is EnumCorpusCostStatus.MISSING
            ),
            absent_run_ids=tuple(i for i in request.run_ids if i not in present),
            skipped_lines=skipped,
        )


__all__ = ["HandlerEvalCorpusReader"]
