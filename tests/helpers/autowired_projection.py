# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Build the REAL auto-wired projection operations for a contract-declared table.

OMN-18774. The kernel path is the seam these tests exist to exercise: a handler
dispatched on a runtime-kernel pod receives ``ProjectionDatabaseOperations``,
whose per-table operation class is chosen from the OWNING CONTRACT's declared
schema domain. Substituting an in-memory adapter or a hand-built target skips
exactly the guard under test -- which is how a sync projection that could not
write its own relation at all stayed green in review for three weeks.

Nothing here overrides the declaration: ``name``, ``schema`` and ``access`` are
read off the shipped ``contract.yaml``, and the domain is resolved through the
same ``omnimarket.projection.relation_domains`` loader the writers use.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from omnibase_core.models.contracts.subcontracts.model_db_table_declaration import (
    ModelDbTableDeclaration,
)
from omnibase_infra.runtime.auto_wiring.handler_wiring import (
    ProjectionDatabaseBindingTarget,
    ProjectionDatabaseTarget,
    ProjectionTableTarget,
    _build_projection_db_adapter,
)

from omnimarket.projection.relation_domains import declared_relation_domain

NODES_DIR = Path(__file__).resolve().parents[2] / "src" / "omnimarket" / "nodes"


def contract_table_declaration(node: str, relation: str) -> ModelDbTableDeclaration:
    """The shipped ``db_io.db_tables`` entry for ``relation``, unmodified."""
    contract = yaml.safe_load(
        (NODES_DIR / node / "contract.yaml").read_text(encoding="utf-8")
    )
    declared = next(
        table for table in contract["db_io"]["db_tables"] if table["name"] == relation
    )
    return ModelDbTableDeclaration(**declared)


def autowired_operations(
    *,
    node: str,
    relation: str,
    dsn: str,
    principal: str,
    physical_database: str,
    dsn_env: str = "OMN18774_PROJECTION_DSN",
) -> Any:
    """The real ``ProjectionDatabaseOperations`` for one declared relation.

    ``principal`` and ``physical_database`` must be the role and database the
    DSN actually resolves to: the binding attests ``current_user`` and
    ``current_database()`` against them on every connection (OMN-16911), so a
    mismatch is refused rather than silently connecting somewhere else.
    """
    table = contract_table_declaration(node, relation)
    binding = ProjectionDatabaseBindingTarget(
        binding_ref="omninode_runtime_service",
        database_ref=table.database_ref,
        physical_database=physical_database,
        principal=principal,
        dsn_env=dsn_env,
    )
    table_target = ProjectionTableTarget(
        table=table,
        database_ref=table.database_ref,
        physical_database=physical_database,
        # These relations live physically in `public` on every real lane --
        # the TENANT domain's schema since OMN-17887, and still the physical
        # home of the internal family bridged until the OMN-15359 cutover.
        physical_schema="public",
        domain=declared_relation_domain(relation),
        read_binding=binding,
        write_binding=binding,
    )
    target = ProjectionDatabaseTarget(
        tables=(table,),
        table_targets=(table_target,),
        physical_database=physical_database,
    )
    return _build_projection_db_adapter(
        {binding.binding_ref: dsn},
        target,
        None,
        None,
    )
