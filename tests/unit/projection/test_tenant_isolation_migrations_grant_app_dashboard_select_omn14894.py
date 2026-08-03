# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Grant-coverage ratchet: every tenant_isolation RLS migration must also grant
app_dashboard SELECT on the same table, in the same migration file.

OMN-14894 grant-gap finding: five of the nine tenant-RLS node migrations
landed by omnimarket#2011 (fix(OMN-15655)) created the ``tenant_isolation``
RLS policy but never granted ``app_dashboard`` SELECT on the table it
protects -- while their three sibling tables in the *same PR*
(context_roi_scores, instruction_eval_aggregate_snapshots,
skill_execution_snapshots) carried the grant. That is an omission, not an
intentional no-reader exception: RLS without the SELECT grant makes the
RLS-scoped dashboard read path completely unreachable for that table (the
role has no way to read the relation at all, policy or not).

This test is the repo-wide enforcement this omission should have been
caught by. It has no allowlist: every migration that creates the
``tenant_isolation`` policy on a table must, in that same file, grant
``app_dashboard`` SELECT on that table. If a table genuinely has no reader
and should stay unreadable, the fix is to not enable RLS with a
dashboard-facing policy in the first place -- not to skip the grant only
DB-role membership would have caught missing.

Pre-fix (this test as first written), five files fail:
  - node_canary_score_reducer/migrations/0002_capability_scores_tenant_id_and_rls.sql
  - node_projection_cost_summary/migrations/0002_llm_cost_aggregates_tenant_id_and_rls.sql
  - node_projection_dep_health/migrations/002_dep_health_findings_tenant_id_and_rls.sql
  - node_projection_pattern_learning/migrations/0001_pattern_learning_artifacts_tenant_id_and_rls.sql
  - node_projection_routing_decision/migrations/0022_agent_routing_decisions_tenant_id_and_rls.sql
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[3]
NODES_ROOT = REPO_ROOT / "src" / "omnimarket" / "nodes"

_IDENTIFIER = r'(?:"[^"]+"|[A-Za-z_][\w$]*)'

_POLICY_RE = re.compile(
    rf"CREATE\s+POLICY\s+tenant_isolation\s+ON\s+(?:{_IDENTIFIER}\.)?(?P<table>{_IDENTIFIER})",
    re.IGNORECASE,
)
_GRANT_SELECT_RE = re.compile(
    rf"GRANT\s+SELECT\s+ON\s+(?:TABLE\s+)?(?:{_IDENTIFIER}\.)?(?P<table>{_IDENTIFIER})\s+TO\s+app_dashboard",
    re.IGNORECASE,
)


def _unquote(name: str) -> str:
    return name.replace('"', "").lower()


def _tenant_isolation_migrations() -> list[Path]:
    return sorted(NODES_ROOT.glob("*/migrations/*.sql"))


def test_every_tenant_isolation_migration_grants_app_dashboard_select() -> None:
    missing: list[str] = []
    for path in _tenant_isolation_migrations():
        text = path.read_text(encoding="utf-8")
        policy_matches = list(_POLICY_RE.finditer(text))
        if not policy_matches:
            continue
        tables = {_unquote(match.group("table")) for match in policy_matches}
        granted_tables = {
            _unquote(match.group("table")) for match in _GRANT_SELECT_RE.finditer(text)
        }
        for table in sorted(tables - granted_tables):
            missing.append(f"{path.relative_to(REPO_ROOT)} (table={table!r})")

    assert not missing, (
        "tenant_isolation RLS migration(s) missing the sibling-pattern "
        "app_dashboard SELECT grant in the same file (OMN-14894): " + "; ".join(missing)
    )


def test_the_five_omn14894_tables_are_covered_by_the_scan() -> None:
    """Pin that the scan actually reaches the five affected files, so a glob
    or regex regression cannot silently turn this into a vacuous pass."""
    expected_tables = {
        "agent_routing_decisions",
        "capability_scores",
        "dep_health_findings",
        "llm_cost_aggregates",
        "pattern_learning_artifacts",
    }
    found_tables: set[str] = set()
    for path in _tenant_isolation_migrations():
        text = path.read_text(encoding="utf-8")
        for policy_match in _POLICY_RE.finditer(text):
            found_tables.add(_unquote(policy_match.group("table")))

    assert expected_tables <= found_tables
