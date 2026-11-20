# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The lookup, Memgraph first (OMN-26104).

A lookup is answered by Cypher from the Memgraph the architecture graph overlay names. Only when
Memgraph is unreachable does it fall back to the file-backed lookup, and the fallback answer names
the typed reason (``EnumLookupFallbackReason``) and the transport error behind it. A statement
Memgraph refuses is a typed ``failed`` answer, never a fallback, and a repository Memgraph holds no
version of is Memgraph's own ``not-found``. The ``users`` question reads the imports as written,
which Memgraph does not hold, so it is answered from the file store with its own typed reason.
"""

from __future__ import annotations

from collections.abc import Callable

from omnimarket.models.model_code_graph_lookup import (
    EnumCodeGraphQuestion,
    EnumLookupFallbackReason,
    EnumLookupServedFrom,
    ModelCodeGraphLookupCommand,
    ModelCodeGraphLookupResult,
)
from omnimarket.nodes.node_code_graph_effect.adapters.memgraph_version_store import (
    MemgraphQueryError,
    MemgraphUnreachableError,
    MemgraphVersionStore,
)
from omnimarket.nodes.node_code_graph_effect.handlers.code_graph_lookup import (
    CodeGraphLookup,
)


class MemgraphFirstLookup:
    """Answer from Memgraph; answer from the fallback, with the typed reason, when it is unreachable."""

    def __init__(
        self,
        *,
        open_store: Callable[[], MemgraphVersionStore],
        fallback: Callable[[ModelCodeGraphLookupCommand], ModelCodeGraphLookupResult],
    ) -> None:
        self._open_store = open_store
        self._fallback = fallback

    def handle(
        self, command: ModelCodeGraphLookupCommand
    ) -> ModelCodeGraphLookupResult:
        if command.question is EnumCodeGraphQuestion.USERS:
            # Memgraph holds no written imports yet: its answer would be a clean negative.
            return self._fallback(command).model_copy(
                update={
                    "served_from": EnumLookupServedFrom.FILE_STORE,
                    "fallback_reason": EnumLookupFallbackReason.QUESTION_NOT_IN_MEMGRAPH,
                    "fallback_detail": "the users question reads the imports as written",
                }
            )
        store: MemgraphVersionStore | None = None
        try:
            store = self._open_store()
            result = CodeGraphLookup(store, store).handle(command)
        except MemgraphUnreachableError as exc:
            return self._fallback(command).model_copy(
                update={
                    "served_from": EnumLookupServedFrom.FILE_STORE,
                    "fallback_reason": exc.reason,
                    "fallback_detail": exc.detail,
                }
            )
        except MemgraphQueryError as exc:
            return ModelCodeGraphLookupResult(
                correlation_id=command.correlation_id,
                question=command.question,
                repo=command.repo,
                status="failed",
                reason=f"memgraph refused the statement: {exc}",
                served_from=EnumLookupServedFrom.MEMGRAPH,
            )
        finally:
            if store is not None:
                store.close()
        return result.model_copy(update={"served_from": EnumLookupServedFrom.MEMGRAPH})
