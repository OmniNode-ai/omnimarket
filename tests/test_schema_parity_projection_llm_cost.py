"""Schema-parity ratchet for node_projection_llm_cost (OMN-13001).

This is the ratchet that prevents the write-model drift class that caused the
original bug (handler wrote columns the deployed table did not have, so every
insert silently landed nothing).

Asserts:
1. The handler write-model column set (``LLM_CALL_METRICS_COLUMNS``) equals the
   INSERT-able column set declared by the node migration that creates
   ``llm_call_metrics`` (every column except the DB-defaulted ``id``).
2. The handler INSERT SQL binds exactly those columns in order.
3. This node no longer writes ``llm_cost_aggregates`` anywhere — the aggregate
   read model is owned by node_projection_cost_summary, not this node.
"""

from __future__ import annotations

import re
from pathlib import Path

from omnimarket.nodes.node_projection_llm_cost.handlers.row_llm_call_metrics import (
    LLM_CALL_METRICS_COLUMNS,
)

NODE_DIR = Path(__file__).resolve().parents[1] / (
    "src/omnimarket/nodes/node_projection_llm_cost"
)
LLM_CALL_METRICS_MIGRATION = (
    NODE_DIR / "migrations" / "0001_create_llm_call_metrics.sql"
)

# Columns the DB fills itself — writers must NOT supply them; they are excluded
# from the parity comparison.
_DB_DEFAULTED_COLUMNS = {"id"}

# Keywords that can start a line inside a CREATE TABLE body but are not column
# definitions.
_NON_COLUMN_TOKENS = {"constraint", "primary", "unique", "check", "foreign"}

# A column definition is `<name> <SQL_TYPE> ...`. We recognize a column by its
# second token being a known SQL type — this rejects CHECK-constraint
# continuation lines (e.g. `prompt_tokens IS NULL OR ...`) whose first token
# happens to be a column name.
_SQL_TYPE_TOKENS = {
    "uuid",
    "varchar",
    "integer",
    "int",
    "bigint",
    "numeric",
    "boolean",
    "jsonb",
    "text",
    "timestamptz",
    "usage_source_type",
    "cost_aggregation_window",
}


def _parse_create_table_columns(sql: str, table: str) -> list[str]:
    """Extract column names from the CREATE TABLE body for ``table``.

    Only top-level ``<name> <SQL_TYPE> ...`` lines are treated as columns;
    constraint lines and CHECK-body continuations are ignored.
    """
    marker = f"CREATE TABLE IF NOT EXISTS {table} ("
    start = sql.index(marker) + len(marker)
    depth = 1
    i = start
    while i < len(sql) and depth > 0:
        if sql[i] == "(":
            depth += 1
        elif sql[i] == ")":
            depth -= 1
        i += 1
    body = sql[start : i - 1]

    columns: list[str] = []
    paren_depth = 0
    for raw_line in body.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("--"):
            continue
        # Only consider lines at the top level of the table body (skip the
        # interior of multi-line CHECK / constraint expressions).
        at_top_level = paren_depth == 0
        paren_depth += line.count("(") - line.count(")")
        if not at_top_level:
            continue
        tokens = line.strip(",").split()
        if len(tokens) < 2:
            continue
        first = tokens[0].strip('"').lower()
        if first in _NON_COLUMN_TOKENS:
            continue
        second_type = tokens[1].strip(",").lower().split("(")[0]
        if second_type not in _SQL_TYPE_TOKENS:
            continue
        columns.append(tokens[0].strip('"'))
    return columns


def test_handler_columns_match_llm_call_metrics_migration() -> None:
    sql = LLM_CALL_METRICS_MIGRATION.read_text(encoding="utf-8")
    migration_columns = _parse_create_table_columns(sql, "llm_call_metrics")
    insertable = [c for c in migration_columns if c not in _DB_DEFAULTED_COLUMNS]

    assert set(LLM_CALL_METRICS_COLUMNS) == set(insertable), (
        "handler write-model columns drifted from the migration. "
        f"handler={sorted(LLM_CALL_METRICS_COLUMNS)} "
        f"migration_insertable={sorted(insertable)}"
    )


def test_handler_insert_sql_binds_the_parity_columns() -> None:
    handler_src = (NODE_DIR / "handlers" / "handler_llm_cost.py").read_text(
        encoding="utf-8"
    )
    insert_match = re.search(
        r"INSERT INTO \{TABLE\} \((.*?)\) VALUES",
        handler_src,
        re.DOTALL,
    )
    assert insert_match is not None, "handler must INSERT INTO llm_call_metrics"
    bound = [c.strip() for c in insert_match.group(1).replace("\n", " ").split(",")]
    bound = [c for c in bound if c]
    assert bound == list(LLM_CALL_METRICS_COLUMNS), (
        "INSERT column order must match LLM_CALL_METRICS_COLUMNS exactly. "
        f"insert={bound} parity={list(LLM_CALL_METRICS_COLUMNS)}"
    )


def test_node_does_not_write_llm_cost_aggregates() -> None:
    """No module in this node may INSERT/UPDATE/upsert llm_cost_aggregates.

    The aggregate read model is owned by node_projection_cost_summary; a write
    here would re-introduce the dual-authority + drift that OMN-13001 removed.
    """
    offenders: list[str] = []
    for py in NODE_DIR.rglob("*.py"):
        if "/tests/" in py.as_posix():
            continue
        text = py.read_text(encoding="utf-8")
        if re.search(
            r"(INSERT INTO llm_cost_aggregates|UPDATE llm_cost_aggregates"
            r"|upsert\(\s*[\"']llm_cost_aggregates)",
            text,
        ):
            offenders.append(py.name)
    assert not offenders, (
        "node_projection_llm_cost must not write llm_cost_aggregates "
        f"(owned by node_projection_cost_summary); offenders={offenders}"
    )
