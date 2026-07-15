# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Real default adapters for HandlerDecisionStoreOrchestrator (OMN-14529).

Before this ticket, ``HandlerDecisionStoreOrchestrator.__init__`` defaulted
all four Protocol adapters (conflict/semantic/store/notify) to ``None``. The
generic ``onex skill`` / receipt-mode dispatch instantiates the
contract-declared handler class with zero arguments (see
``node_dod_sweep_orchestrator`` for the working pattern this module follows),
so a live dispatch always hit ``RuntimeError("... adapter required ...")``
before a single row could be written — the routing gap and the adapter gap
were two distinct bugs stacked on top of each other.

These adapters give the handler real, working defaults:

- :class:`PostgresDecisionStore` — persists/queries ``decision_store``
  by reusing the EXISTING ``omnibase_infra`` Postgres handlers
  (``HandlerWriteDecision`` / ``HandlerQueryDecisions``) rather than
  re-implementing persistence.
- :class:`StructuralConflictCheck` — real structural conflict
  detection reusing the pure ``structural_confidence()`` function already
  implemented in ``omnibase_infra``.
- :class:`DeferredSemanticReview` — honest pass-through. Semantic
  (LLM) conflict review is genuinely out of scope for this ticket; this
  adapter never claims to have run a check it didn't run.
- :class:`LogOnlyNotify` — logs HIGH-severity conflicts instead of
  posting to Slack. The Slack conflict-resolution gate described in
  ``decision_store``'s SKILL.md is not implemented anywhere in this repo;
  this adapter is an honest placeholder, not a facade.
"""

from __future__ import annotations

from omnimarket.nodes.node_decision_store_orchestrator.adapters.deferred_semantic_review import (
    DeferredSemanticReview,
)
from omnimarket.nodes.node_decision_store_orchestrator.adapters.log_only_notify import (
    LogOnlyNotify,
)
from omnimarket.nodes.node_decision_store_orchestrator.adapters.postgres_decision_store import (
    PostgresDecisionStore,
)
from omnimarket.nodes.node_decision_store_orchestrator.adapters.structural_conflict_check import (
    StructuralConflictCheck,
)

__all__ = [
    "DeferredSemanticReview",
    "LogOnlyNotify",
    "PostgresDecisionStore",
    "StructuralConflictCheck",
]
