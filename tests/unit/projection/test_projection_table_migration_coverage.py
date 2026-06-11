"""Enforcement ratchet: every projection-API read model must be created in the
projection database by a node-owned migration (OMN-12970).

FAILURE MODE THIS GUARDS
    The projection API binds to the dashboard projection DB (omnidash_analytics).
    A node contract can declare ``projection_api.expose: true`` with a ``table``
    that is created ONLY by an omnibase_infra forward migration in the
    omnibase_infra DB — never in the projection DB. At startup the projection API
    then marks the topic DEGRADED ("table '<schema>.<table>' not found at
    startup") and the dashboard panel renders empty. This is exactly how the
    ab-compare panel broke: node_ab_compare_reducer declared
    public.llm_call_metrics, but no node shipped a migration to create it in
    omnidash_analytics.

INVARIANT
    For every contract with ``projection_api.expose: true`` whose declared table
    lives in the ``public`` schema, some node under
    ``src/omnimarket/nodes/*/migrations/*.sql`` must contain a
    ``CREATE TABLE ... <table>`` (or ``CREATE ... VIEW ... <table>``) statement.
    Node migrations are the only artifacts vendored into the projection DB by
    ``omnibase_infra/scripts/sync-node-migrations.sh`` +
    ``run-forward-migrations.sh``.

    Tables NOT owned by an omnimarket node migration must be listed in
    ``_EXTERNALLY_OWNED_PROJECTION_TABLES`` with the owning migration cited, so
    every exception is explicit and reviewed.

    ``omnidash_analytics``-schema tables are out of scope here: they are produced
    by the omnidash TypeScript read-model runner, not the node migration path.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from omnimarket.projection.discovery import build_projection_topic_map

_NODES_DIR = Path(__file__).resolve().parents[2].parent / "src" / "omnimarket" / "nodes"

# Tables a projection contract may declare in the ``public`` schema that are NOT
# created by an omnimarket node migration. Each entry MUST cite the migration
# that owns the table in the projection database. Adding an entry is a reviewed
# exception, never a silent escape hatch.
_EXTERNALLY_OWNED_PROJECTION_TABLES: dict[str, str] = {}


def _node_migration_create_targets() -> set[str]:
    """Collect every table/view name created by any node-owned migration SQL."""
    targets: set[str] = set()
    create_re = re.compile(
        r"CREATE\s+(?:OR\s+REPLACE\s+)?"
        r"(?:TABLE|MATERIALIZED\s+VIEW|VIEW)\s+"
        r"(?:IF\s+NOT\s+EXISTS\s+)?"
        r'(?:"?public"?\.)?'  # optional schema qualifier
        r'"?(?P<name>[a-zA-Z_][a-zA-Z0-9_]*)"?',
        re.IGNORECASE,
    )
    for sql_path in _NODES_DIR.glob("*/migrations/*.sql"):
        text = sql_path.read_text(encoding="utf-8")
        for match in create_re.finditer(text):
            targets.add(match.group("name").lower())
    return targets


def _public_projection_tables() -> dict[str, str]:
    """Map declared public-schema projection table -> first topic that needs it."""
    topic_map = build_projection_topic_map()
    needed: dict[str, str] = {}
    for topic, cfg in topic_map.items():
        if cfg.schema_name != "public":
            continue
        needed.setdefault(cfg.table.lower(), topic)
    return needed


@pytest.mark.unit
def test_every_public_projection_table_has_a_node_migration() -> None:
    """Each public-schema projection read model is created by a node migration."""
    created = _node_migration_create_targets()
    needed = _public_projection_tables()

    missing: list[str] = []
    for table, topic in sorted(needed.items()):
        if table in created:
            continue
        if table in _EXTERNALLY_OWNED_PROJECTION_TABLES:
            continue
        missing.append(
            f"table 'public.{table}' (declared by projection topic {topic!r}) "
            f"is not created by any src/omnimarket/nodes/*/migrations/*.sql and "
            f"is not in _EXTERNALLY_OWNED_PROJECTION_TABLES"
        )

    assert not missing, (
        "Projection contracts declare public-schema tables with no node-owned "
        "migration to create them in the projection database "
        "(omnidash_analytics). The projection API will mark these topics "
        "DEGRADED at startup. Ship a node migration under the owning node's "
        "migrations/ directory, or cite the external owner in "
        "_EXTERNALLY_OWNED_PROJECTION_TABLES.\n  - " + "\n  - ".join(missing)
    )


@pytest.mark.unit
def test_llm_call_metrics_migration_present() -> None:
    """Regression lock for OMN-12970: the ab-compare table migration exists."""
    created = _node_migration_create_targets()
    assert "llm_call_metrics" in created, (
        "node-owned migration creating public.llm_call_metrics is missing; "
        "ab-compare / cost token-usage projections will DEGRADE in the "
        "projection database"
    )


@pytest.mark.unit
def test_externally_owned_allowlist_entries_are_actually_declared() -> None:
    """Allowlist may not carry stale entries for tables no contract declares."""
    needed = set(_public_projection_tables())
    stale = sorted(set(_EXTERNALLY_OWNED_PROJECTION_TABLES) - needed)
    assert not stale, (
        "_EXTERNALLY_OWNED_PROJECTION_TABLES lists tables that no projection "
        f"contract declares as a public-schema read model: {stale}. Remove them."
    )
