# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Grant-coverage ratchet: every tenant_isolation RLS policy must be followed, in
the same node's migration chain, by an app_dashboard SELECT grant on the same table.

The chain is the node's migration files in apply order. A migration that is already
applied is immutable (a vendored copy in omnibase_infra, byte-identical, is what
runs), so the grant may sit in the same file as the policy or in a later migration of
the chain; it may not sit in an earlier one, and it may not be missing.

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
caught by. It has no allowlist: every table with a ``tenant_isolation`` policy
must, in that file or a later migration of the same node, be granted
``app_dashboard`` SELECT. If a table genuinely has no reader
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


def _missing_grants(migrations: list[Path]) -> list[str]:
    """Tables whose policy has no app_dashboard SELECT grant at or after it in the node chain."""
    by_node: dict[Path, list[Path]] = {}
    for path in migrations:
        by_node.setdefault(path.parent, []).append(path)
    missing: list[str] = []
    for chain in by_node.values():
        for index, path in enumerate(sorted(chain)):
            text = path.read_text(encoding="utf-8")
            tables = {_unquote(m.group("table")) for m in _POLICY_RE.finditer(text)}
            granted: set[str] = set()
            for later in sorted(chain)[index:]:
                granted |= {
                    _unquote(m.group("table"))
                    for m in _GRANT_SELECT_RE.finditer(
                        later.read_text(encoding="utf-8")
                    )
                }
            for table in sorted(tables - granted):
                missing.append(f"{path.relative_to(REPO_ROOT)} (table={table!r})")
    return missing


def test_every_tenant_isolation_migration_grants_app_dashboard_select() -> None:
    missing = _missing_grants(_tenant_isolation_migrations())
    assert not missing, (
        "tenant_isolation RLS policy without an app_dashboard SELECT grant in the "
        "same file or a later migration of the node (OMN-14894): " + "; ".join(missing)
    )


def test_the_grant_must_not_precede_the_policy_in_the_chain(tmp_path: Path) -> None:
    """Negative control: a grant in an EARLIER migration does not cover a later policy,
    and a policy with no grant anywhere is reported."""
    chain = tmp_path / "node_x" / "migrations"
    chain.mkdir(parents=True)
    early = chain / "0001_grant.sql"
    early.write_text("GRANT SELECT ON public.t TO app_dashboard;\n")
    late = chain / "0002_policy.sql"
    late.write_text(
        "CREATE POLICY tenant_isolation ON public.t FOR ALL USING (true);\n"
    )
    global REPO_ROOT
    original = REPO_ROOT
    REPO_ROOT = tmp_path
    try:
        assert _missing_grants([early, late]) == [
            "node_x/migrations/0002_policy.sql (table='t')"
        ]
        late.write_text(
            late.read_text() + "GRANT SELECT ON public.t TO app_dashboard;\n"
        )
        assert _missing_grants([early, late]) == []
    finally:
        REPO_ROOT = original


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
