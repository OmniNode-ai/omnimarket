"""Tested-constant guarantees for the 7->4 dispatch-role mapping (OMN-15163).

The mapping table is the mechanism the handler uses to decide which of the 4
omnibase_core report models a raw dispatch-worker report payload must be
validated against -- see model_role_mapping.py's module docstring for the
per-role rationale. These tests lock:

1. The table (+ the explicit unmappable set) is TOTAL over all 7
   node_dispatch_worker roles and the two sets are disjoint (no role is both
   mapped and declared out-of-scope, and no role is neither).
2. The locally-mirrored EnumDispatchWorkerRole values never silently drift
   from node_dispatch_worker's real EnumWorkerRole (the two are separate
   StrEnum definitions by CLAUDE.md's node-isolation rule; this is the CI
   tripwire for that duplication).
3. The specific mapping decisions this ticket documents (fixer->implementer,
   the 5 read-only roles ->scout, ops declared unmappable, and that nothing
   maps to verifier/lander from this table).
"""

from __future__ import annotations

from omnibase_core.enums.enum_dispatch_report_role import EnumDispatchReportRole

from omnimarket.nodes.node_dispatch_worker.models.model_dispatch_worker_command import (
    EnumWorkerRole,
)
from omnimarket.nodes.node_report_validation_compute.models.model_dispatch_worker_role import (
    EnumDispatchWorkerRole,
)
from omnimarket.nodes.node_report_validation_compute.models.model_role_mapping import (
    ROLE_MAPPING_TABLE,
    UNMAPPABLE_DISPATCH_ROLES,
    resolve_report_role,
)


def test_mapping_table_is_total_and_disjoint_from_unmappable() -> None:
    all_roles = set(EnumDispatchWorkerRole)
    mapped = set(ROLE_MAPPING_TABLE)
    unmappable = set(UNMAPPABLE_DISPATCH_ROLES)

    assert mapped & unmappable == set(), "a role cannot be both mapped and unmappable"
    assert mapped | unmappable == all_roles, "every dispatch role must be classified"


def test_local_role_enum_values_match_node_dispatch_worker_source_of_truth() -> None:
    """Drift tripwire: the two node-local EnumWorkerRole/EnumDispatchWorkerRole
    definitions must always carry the same 7 string values."""
    local_values = {role.value for role in EnumDispatchWorkerRole}
    source_values = {role.value for role in EnumWorkerRole}
    assert local_values == source_values


def test_fixer_maps_to_implementer() -> None:
    assert (
        resolve_report_role(EnumDispatchWorkerRole.fixer)
        is EnumDispatchReportRole.IMPLEMENTER
    )


def test_read_only_roles_default_to_scout() -> None:
    for role in (
        EnumDispatchWorkerRole.watcher,
        EnumDispatchWorkerRole.designer,
        EnumDispatchWorkerRole.auditor,
        EnumDispatchWorkerRole.synthesizer,
        EnumDispatchWorkerRole.sweep,
    ):
        assert resolve_report_role(role) is EnumDispatchReportRole.SCOUT, role


def test_ops_is_declared_unmappable() -> None:
    assert EnumDispatchWorkerRole.ops in UNMAPPABLE_DISPATCH_ROLES
    assert resolve_report_role(EnumDispatchWorkerRole.ops) is None


def test_nothing_maps_to_verifier_or_lander() -> None:
    mapped_report_roles = set(ROLE_MAPPING_TABLE.values())
    assert EnumDispatchReportRole.VERIFIER not in mapped_report_roles
    assert EnumDispatchReportRole.LANDER not in mapped_report_roles
