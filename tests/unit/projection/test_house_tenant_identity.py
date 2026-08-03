# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The house tenant has exactly ONE identity, and it is derived, not typed.

OPERATOR RULING 2026-08-02: OmniNode is a first-class tenant with its own fixed
tenant id. "Fixed" is only worth anything if it is checkable, so:

* :data:`HOUSE_TENANT_UUID` is a UUIDv5, re-derived here from its namespace and
  name. A hand-edited literal fails this test instead of quietly becoming a new
  tenant.
* the slug and the UUID are two representations of one identity, and this file
  is the place that says which is which -- the slug is what every landed
  ``TEXT`` column stores today, the UUID is the ADR-0027 canonical identifier
  OMN-15356 converts the whole set to in one pass.
* the divergent third value that used to exist in-tree (``"default"``, written
  by ``handler_budget_state``) is asserted gone. That constant was the only
  thing deciding where an unattributed budget row landed, and it pointed at a
  tenant present in no registry, no policy and no other relation.
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Final

from omnimarket.nodes.node_projection_delegation.handlers.handler_budget_state import (
    DEFAULT_TENANT as BUDGET_STATE_DEFAULT_TENANT,
)
from omnimarket.nodes.node_projection_delegation_inference_response.models.model_inference_response_projection import (
    DEFAULT_TENANT as INFERENCE_RESPONSE_DEFAULT_TENANT,
)
from omnimarket.projection.tenant_isolation import (
    HOUSE_TENANT_SLUG,
    HOUSE_TENANT_UUID,
    INTERIM_DEFAULT_TENANT,
)

_UUID_NAME: Final[str] = "house-tenant.omninode.ai"
_REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[3]


def test_the_house_tenant_uuid_is_derived_not_typed() -> None:
    """Re-derive the pinned UUID from first principles."""
    assert uuid.uuid5(uuid.NAMESPACE_DNS, _UUID_NAME) == HOUSE_TENANT_UUID
    assert HOUSE_TENANT_UUID.version == 5
    assert HOUSE_TENANT_UUID.int != 0


def test_every_in_tree_tenant_default_is_the_house_tenant() -> None:
    """One identity. The pre-ruling alias and both node constants agree."""
    assert HOUSE_TENANT_SLUG == "omninode"
    assert INTERIM_DEFAULT_TENANT == HOUSE_TENANT_SLUG
    assert BUDGET_STATE_DEFAULT_TENANT == HOUSE_TENANT_SLUG, (
        "handler_budget_state carried the orphan tenant 'default'; the "
        "house-tenant ruling settled that there is exactly one house tenant"
    )
    assert INFERENCE_RESPONSE_DEFAULT_TENANT == HOUSE_TENANT_SLUG


def test_the_orphan_default_tenant_literal_is_gone_from_the_budget_writer() -> None:
    """No writer may reintroduce a tenant id that exists nowhere else.

    Source-level, because the defect was a module constant: an import-level
    equality check passes the moment someone re-adds the literal at a
    different call site.
    """
    source = (
        _REPO_ROOT
        / "src/omnimarket/nodes/node_projection_delegation/handlers/handler_budget_state.py"
    ).read_text(encoding="utf-8")
    code = "\n".join(
        line for line in source.splitlines() if not line.lstrip().startswith("#")
    )
    orphan_literals = [token for token in ('"default"', "'default'") if token in code]
    assert not orphan_literals, (
        "the orphan tenant literal is back in the budget-state writer; "
        "delegation_budget_state.tenant_id is TEXT NOT NULL with no column "
        "default, so this constant alone decides where unattributed rows land"
    )


def test_the_tenant_classified_relations_carry_a_tenant_id_migration() -> None:
    """Every relation this ruling classified TENANT got its column.

    Guards the half-landed shape: a contract flipped to ``schema: tenant``
    whose table has no ``tenant_id`` is a relation the RLS policy cannot
    isolate and the house tenant cannot own.
    """
    expected = {
        "node_canary_score_reducer": "capability_scores",
        "node_projection_context_roi": "context_roi_scores",
        "node_projection_cost_summary": "llm_cost_aggregates",
        "node_projection_dep_health": "dep_health_findings",
        "node_projection_instruction_eval": "instruction_eval_aggregate_snapshots",
        "node_projection_pattern_learning": "pattern_learning_artifacts",
        "node_projection_routing_decision": "agent_routing_decisions",
        "node_projection_skill_executions": "skill_execution_snapshots",
    }
    nodes_root = _REPO_ROOT / "src/omnimarket/nodes"
    missing: list[str] = []
    for node, table in expected.items():
        migrations = sorted((nodes_root / node / "migrations").glob("*.sql"))
        blob = "\n".join(p.read_text(encoding="utf-8") for p in migrations)
        has_column = (
            f"ADD COLUMN IF NOT EXISTS tenant_id TEXT NOT NULL DEFAULT "
            f"'{HOUSE_TENANT_SLUG}'" in blob
        )
        has_policy = "CREATE POLICY tenant_isolation ON" in blob
        has_force = "FORCE ROW LEVEL SECURITY" in blob
        if not (has_column and has_policy and has_force):
            missing.append(
                f"{node}/{table}: column={has_column} policy={has_policy} "
                f"force={has_force}"
            )
    assert not missing, (
        "tenant-classified relations missing their tenant_id column, "
        f"tenant_isolation policy, or FORCE RLS: {missing}"
    )
