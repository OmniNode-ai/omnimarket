# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Derive which golden chains are provable from omnimarket CI alone (OMN-14536).

The golden-chain gate was vacuous because CI supplied ``projected_rows={}`` and
gated on the *workflow* running, not on the *node's verdict* — 0 of 13 chains
were ever verified. Fixing the gate requires a **harness-collected census** for
the chains omnimarket can actually drive end-to-end with zero infra, and a
**derived** routing of the rest to the repo where their runtime lives.

This module owns the split. It is **computed**, never a hand-maintained list:

Group A — omnimarket-CI provable
    An in-repo projection node closes the chain: it writes the chain's
    ``tail_table`` (a real DB table) and its contract references the chain's
    ``head_topic``, AND its handler exposes a **synchronous** ``project(event,
    db)`` seam. Only then can the census collector drive head->tail on an
    ``InmemoryDatabaseAdapter`` with no Kafka and no Postgres.

Group B — routed to omnibase_infra CI
    Everything the omnimarket harness cannot prove in-memory:
      * no in-repo projection closes the chain (its projection lives elsewhere),
      * the tail is an event-bus surface (``event_bus:...``), not a table,
      * the projection is async / raw-SQL (needs a live Postgres), or
      * the head is produced only cross-repo (the delegation runtime lives in
        omnibase_infra).
    These are proven in omnibase_infra CI, against the live delegation runtime.

``census_collector`` cross-checks its fixtures against this oracle and fails
closed on any disagreement, so the fixture set can never silently add or drop a
chain the derivation did not classify.
"""

from __future__ import annotations

import importlib
import inspect
from functools import lru_cache
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field

from omnimarket.nodes.node_golden_chain_sweep.handlers.handler_golden_chain_sweep import (
    ModelChainDefinition,
)

# The node package lives at src/omnimarket/nodes/node_golden_chain_sweep/; the
# repo's node tree is three parents up (…/nodes/).
_NODES_ROOT = Path(__file__).resolve().parent.parent


class ModelChainReachability(BaseModel):
    """The derived Group A / Group B split over the golden-chain registry."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    reachable: list[str] = Field(
        default_factory=list,
        description="Group A — chains omnimarket CI can drive head->tail in-memory.",
    )
    routed: list[str] = Field(
        default_factory=list,
        description="Group B — chains routed to omnibase_infra CI (unreachable here).",
    )
    reasons: dict[str, str] = Field(
        default_factory=dict,
        description="Per-chain one-line justification for its classification.",
    )

    @property
    def coverage_boundary(self) -> str:
        """Human-readable coverage boundary statement (for the CI gate log)."""
        return (
            f"omnimarket golden_chain gate COVERS {len(self.reachable)} locally-"
            f"reachable chain(s): {sorted(self.reachable)}. "
            f"ROUTED to omnibase_infra CI (cross-repo/runtime-dependent, "
            f"{len(self.routed)}): {sorted(self.routed)}."
        )


@lru_cache(maxsize=1)
def _scan_node_contracts() -> tuple[dict[str, object], ...]:
    """Scan every in-repo node contract once.

    Returns a tuple of per-node dicts: ``{node, text, tables, handler_module,
    handler_class}``. Tuple (not list) so the result is hashable for the cache.
    """
    out: list[dict[str, object]] = []
    for cpath in sorted(_NODES_ROOT.glob("*/contract.yaml")):
        node = cpath.parent.name
        text = cpath.read_text()
        try:
            c = yaml.safe_load(text) or {}
        except yaml.YAMLError:
            continue
        tables: list[str] = []
        for tb in (c.get("db_io", {}) or {}).get("db_tables", []) or []:
            name = tb.get("name") if isinstance(tb, dict) else tb
            if name:
                tables.append(str(name))
        handler = c.get("handler", {}) or {}
        out.append(
            {
                "node": node,
                "text": text,
                "tables": tables,
                "handler_module": handler.get("module", ""),
                "handler_class": handler.get("class", ""),
            }
        )
    return tuple(out)


def _has_sync_project_seam(handler_module: str, handler_class: str) -> bool:
    """True when the projection handler exposes a synchronous ``project`` method.

    Async-only projections (``project_event``) and raw-SQL projections cannot be
    driven on the InmemoryDatabaseAdapter, so they are routed to omnibase_infra.
    Import failures fail closed (treated as not-drivable).
    """
    if not handler_module or not handler_class:
        return False
    try:
        mod = importlib.import_module(handler_module)
        cls = getattr(mod, handler_class, None)
    except Exception:
        return False
    if cls is None:
        return False
    proj = getattr(cls, "project", None)
    return callable(proj) and not inspect.iscoroutinefunction(proj)


def _closing_projection(
    chain: ModelChainDefinition,
) -> dict[str, object] | None:
    """Return the in-repo node that closes ``chain``, or None.

    A node closes the chain when it writes the chain's ``tail_table`` and its
    contract text references the chain's ``head_topic`` (the source topic the
    projection consumes). Both facts come from the node's own contract, so this
    is contract-derived, not a hand-list.
    """
    tail = chain.tail_table
    if tail.startswith("event_bus:"):
        # An event-bus tail surface is not a materialized table — nothing for the
        # in-memory projection harness to read back. Route to infra.
        return None
    for node in _scan_node_contracts():
        tables = node["tables"]
        if not isinstance(tables, list):
            continue
        if tail in tables and chain.head_topic in str(node["text"]):
            return node
    return None


def derive_chain_reachability(
    chains: list[ModelChainDefinition],
) -> ModelChainReachability:
    """Compute the Group A / Group B split over ``chains`` from contracts."""
    reachable: list[str] = []
    routed: list[str] = []
    reasons: dict[str, str] = {}

    for chain in chains:
        node = _closing_projection(chain)
        if node is None:
            routed.append(chain.name)
            if chain.tail_table.startswith("event_bus:"):
                reasons[chain.name] = (
                    f"tail '{chain.tail_table}' is an event-bus surface, not a "
                    "materialized table — proven in omnibase_infra runtime CI"
                )
            else:
                reasons[chain.name] = (
                    f"no in-repo projection writes '{chain.tail_table}' while "
                    f"consuming '{chain.head_topic}' — closed cross-repo, routed "
                    "to omnibase_infra CI"
                )
            continue

        if not _has_sync_project_seam(
            str(node["handler_module"]), str(node["handler_class"])
        ):
            routed.append(chain.name)
            reasons[chain.name] = (
                f"projection '{node['node']}' has no synchronous project() seam "
                "(async/raw-SQL) — not in-memory drivable, routed to omnibase_infra CI"
            )
            continue

        reachable.append(chain.name)
        reasons[chain.name] = (
            f"projection '{node['node']}' closes the chain with a sync project() "
            "seam — omnimarket CI drives it head->tail in-memory"
        )

    return ModelChainReachability(reachable=reachable, routed=routed, reasons=reasons)


__all__ = ["ModelChainReachability", "derive_chain_reachability"]
