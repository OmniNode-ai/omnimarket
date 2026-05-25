# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Pure reducer: accumulates knowledge context fragments and materializes a bundle.

reduce(state, fragment) -> new_state
materialize(state) -> ModelKnowledgeContextBundle | None

No I/O. Callers own the state and pass it in on every invocation.
"""

from __future__ import annotations

import logging
from typing import Literal

from omnimarket.nodes.node_knowledge_context_assembler_reducer.models.model_knowledge_context_bundle import (
    EnumBundleStatus,
    ModelKnowledgeContextBundle,
)
from omnimarket.nodes.node_knowledge_context_assembler_reducer.models.model_knowledge_context_fragment import (
    ModelKnowledgeContextFragment,
)
from omnimarket.nodes.node_knowledge_context_assembler_reducer.models.model_knowledge_context_state import (
    ModelKnowledgeContextState,
)

logger = logging.getLogger(__name__)

__all__ = ["HandlerKnowledgeContextAssemblerReducer"]


class HandlerKnowledgeContextAssemblerReducer:
    """Pure reducer that accumulates backend fragments and materializes a context bundle."""

    handler_type: Literal["node_handler"] = "node_handler"
    handler_category: Literal["reducer"] = "reducer"

    def accumulate(
        self,
        state: ModelKnowledgeContextState,
        fragment: ModelKnowledgeContextFragment,
    ) -> ModelKnowledgeContextState:
        """Add one backend fragment to the accumulation state.

        Idempotent: duplicate fragments for the same fragment_source are ignored.
        """
        if state.completed:
            logger.debug(
                "Ignoring fragment for completed correlation_id=%s source=%s",
                state.correlation_id,
                fragment.fragment_source,
            )
            return state

        if fragment.correlation_id != state.correlation_id:
            logger.warning(
                "correlation_id mismatch: state=%s fragment=%s — ignoring",
                state.correlation_id,
                fragment.correlation_id,
            )
            return state

        existing_sources = {f.fragment_source for f in state.fragments}
        if fragment.fragment_source in existing_sources:
            logger.debug(
                "Duplicate fragment source=%s correlation_id=%s — ignoring",
                fragment.fragment_source,
                state.correlation_id,
            )
            return state

        new_fragments = (*state.fragments, fragment)
        completed = len(new_fragments) >= state.expected_count

        logger.info(
            "accumulated fragment source=%s correlation_id=%s (%d/%d)",
            fragment.fragment_source,
            state.correlation_id,
            len(new_fragments),
            state.expected_count,
        )

        return state.model_copy(
            update={"fragments": new_fragments, "completed": completed}
        )

    def materialize(
        self,
        state: ModelKnowledgeContextState,
    ) -> ModelKnowledgeContextBundle | None:
        """Materialize the bundle when all expected fragments have arrived.

        Returns None if not yet complete.
        """
        if not state.completed:
            return None

        ok_count = sum(1 for f in state.fragments if f.ok)
        total = len(state.fragments)

        if ok_count == total:
            status = EnumBundleStatus.COMPLETE
        elif ok_count == 0:
            status = EnumBundleStatus.DEGRADED
        else:
            status = EnumBundleStatus.PARTIAL

        return ModelKnowledgeContextBundle(
            correlation_id=state.correlation_id,
            status=status,
            fragments=state.fragments,
            fragment_count=total,
        )
