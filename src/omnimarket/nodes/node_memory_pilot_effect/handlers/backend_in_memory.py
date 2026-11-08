# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""In-memory backend: the default, and the stage-one baseline when a lab model answers."""

from __future__ import annotations

from omnimarket.nodes.node_memory_pilot_effect.handlers.lexical_retrieval import (
    render_context,
    retrieve,
)
from omnimarket.nodes.node_memory_pilot_effect.models.model_memory_pilot import (
    ModelMemoryPilotDocument,
)
from omnimarket.nodes.node_memory_pilot_effect.protocols.protocol_memory_pilot import (
    BackendAnswer,
    ProtocolChatAnswerer,
)

NOT_IN_CONTEXT = "NOT IN CONTEXT"


class BackendInMemory:
    """Lexical retrieval over documents held in a dict; no files, no network.

    With an answerer the lab chat model writes the answer from the retrieved
    context (the stage-one baseline). Without one the best paragraph is the
    answer, so the default needs no model at all.
    """

    def __init__(
        self,
        *,
        name: str = "in_memory",
        answerer: ProtocolChatAnswerer | None = None,
        system_prompt: str = "",
    ) -> None:
        self.name = name
        self._answerer = answerer
        self._system_prompt = system_prompt
        self._datasets: dict[str, tuple[ModelMemoryPilotDocument, ...]] = {}

    async def ingest(
        self, dataset: str, documents: tuple[ModelMemoryPilotDocument, ...]
    ) -> int:
        self._datasets[dataset] = (*self._datasets.get(dataset, ()), *documents)
        return len(documents)

    async def query(self, dataset: str, question: str) -> BackendAnswer:
        documents = self._datasets.get(dataset)
        if documents is None:
            raise KeyError(dataset)
        refs = retrieve(documents, question)
        if self._answerer is None:
            return BackendAnswer(refs[0].excerpt if refs else NOT_IN_CONTEXT, refs)
        answer = await self._answerer.answer(
            self._system_prompt, question, render_context(refs)
        )
        return BackendAnswer(answer, refs)
