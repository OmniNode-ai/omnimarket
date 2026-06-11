#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""CI gate: node migration sets must create base tables before views (OMN-12942).

The omnimarket forward-migration runner applies each node's
``src/omnimarket/nodes/<node>/migrations/*.sql`` files in lexical filename order
under a node-scoped identity space (``run-forward-migrations.sh`` /
``run-projection-migrations.py``). A node migration that ``CREATE ... VIEW`` over
a base table the node never creates — or creates LATER in the lexical order — is
a latent hard-fail: a clean forward run of the node set alone aborts on a missing
relation. That is exactly the defect that forced ``node_projection_llm_routing``'s
view migration to be pruned during the 2026-06-11 stability hot-patch (the view
referenced ``llm_routing_decisions``, which only existed in the flat infra
sequence, not in the node's own migration set).

This gate makes the defect class unshippable. For every node migration directory:

  1. Walk the ``*.sql`` files in lexical order.
  2. Track the set of base tables created so far (``CREATE TABLE [IF NOT EXISTS]``).
  3. For every ``CREATE [OR REPLACE] VIEW``, collect the relations it reads from
     (``FROM`` / ``JOIN`` targets).
  4. Each referenced relation must be either: a view/table created earlier in the
     node's own ordered set, OR an entry in the explicit cross-source allowlist
     (a table owned by another node or the flat infra sequence, with provenance).

A relation referenced by a view but neither created in-node-earlier nor
allowlisted is a FAIL. Adding a base table to the wrong (later) position in the
node's lexical order is also a FAIL.

Allowlisting a cross-source base table is intentional friction: the entry must
name the owning migration so the cross-node apply-order guarantee is auditable.

Exit codes: 0 = clean; 1 = ordering/missing-base-table violations; 2 = invocation error.
"""

from __future__ import annotations

import argparse
import pathlib
import re
import sys

# Relations a node view may read that are created OUTSIDE the node's own
# migration set. Each entry maps relation -> the owning migration that
# guarantees the relation exists before any node view phase runs.
#
# The forward-migration runner applies the flat infra sequence (phase 1) and the
# delegation node (lexically before savings) BEFORE the consuming view, so these
# cross-source reads are apply-order-safe. New entries require the owning
# migration path so the guarantee stays auditable — do NOT add a bare relation.
CROSS_SOURCE_BASE_TABLES: dict[str, str] = {
    # delegation_events is owned by node_projection_delegation; that node sorts
    # lexically before node_projection_savings, so its base table is applied
    # before node_projection_savings/076 reads it.
    "delegation_events": (
        "src/omnimarket/nodes/node_projection_delegation/migrations/"
        "0007_delegation_events.sql"
    ),
}

_CREATE_TABLE_RE = re.compile(
    r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?"
    r'"?(?P<name>[a-zA-Z_][a-zA-Z0-9_]*)"?',
    re.IGNORECASE,
)
_CREATE_VIEW_RE = re.compile(
    r"CREATE\s+(?:OR\s+REPLACE\s+)?(?:MATERIALIZED\s+)?VIEW\s+"
    r'"?(?P<name>[a-zA-Z_][a-zA-Z0-9_]*)"?',
    re.IGNORECASE,
)
# Relations read by a query: FROM <rel> / JOIN <rel>. Excludes derived-table
# aliases (a relation immediately preceded by ``(`` is a subquery, not a name).
_FROM_JOIN_RE = re.compile(
    r"\b(?:FROM|JOIN)\s+"
    r'(?!\()"?(?P<name>[a-zA-Z_][a-zA-Z0-9_]*)"?',
    re.IGNORECASE,
)

# CTE names declared via WITH ... AS / , <name> AS ( are local relations, not
# base tables; collect them so a view reading its own CTE is not flagged.
_CTE_RE = re.compile(
    r"(?:WITH|,)\s+(?P<name>[a-zA-Z_][a-zA-Z0-9_]*)\s+AS\s*\(",
    re.IGNORECASE,
)

# SQL keywords / set-operation tokens that can follow FROM/JOIN but are not
# relation names.
_NON_RELATION_TOKENS = frozenset(
    {"select", "lateral", "unnest", "generate_series", "jsonb_array_elements"}
)


def _strip_comments(sql: str) -> str:
    """Remove ``--`` line comments so commented relation names are ignored."""
    return "\n".join(line.split("--", 1)[0] for line in sql.splitlines())


# EXTRACT(<field> FROM <expr>) uses FROM as a syntactic separator, not a relation
# source. Replace it with a single space before relation scanning so the inner
# FROM never matches the FROM/JOIN relation regex.
_EXTRACT_RE = re.compile(r"EXTRACT\s*\([^()]*\)", re.IGNORECASE)


def _referenced_relations(view_body: str) -> set[str]:
    body = _strip_comments(view_body)
    body = _EXTRACT_RE.sub(" ", body)
    ctes = {m.group("name").lower() for m in _CTE_RE.finditer(body)}
    refs: set[str] = set()
    for match in _FROM_JOIN_RE.finditer(body):
        name = match.group("name").lower()
        if name in _NON_RELATION_TOKENS or name in ctes:
            continue
        refs.add(name)
    return refs


def _check_node(node_dir: pathlib.Path, repo_root: pathlib.Path) -> list[str]:
    """Return human-readable violation strings for one node migrations dir."""
    violations: list[str] = []
    created: set[str] = set()
    allow = {k.lower() for k in CROSS_SOURCE_BASE_TABLES}
    for sql_file in sorted(node_dir.glob("*.sql"), key=lambda f: f.name):
        try:
            sql = sql_file.read_text(encoding="utf-8")
        except OSError as exc:  # pragma: no cover - filesystem race
            violations.append(f"{sql_file}: unreadable ({exc})")
            continue
        clean = _strip_comments(sql)
        rel_path = sql_file.relative_to(repo_root)

        # Views in this file must only read already-created or allowlisted rels.
        for view_match in _CREATE_VIEW_RE.finditer(clean):
            view_name = view_match.group("name").lower()
            # The view body is everything from this CREATE VIEW to the next
            # statement-terminating semicolon at depth 0; a simple slice to the
            # next ``;`` after the match is sufficient for these single-statement
            # view files and avoids over-reading sibling statements.
            tail = clean[view_match.end() :]
            semi = tail.find(";")
            body = tail if semi == -1 else tail[:semi]
            for rel in _referenced_relations(body):
                if rel in created or rel in allow or rel == view_name:
                    continue
                violations.append(
                    f"{rel_path}: view '{view_name}' reads relation '{rel}' "
                    f"which is not created earlier in node '{node_dir.parent.name}' "
                    f"and is not in CROSS_SOURCE_BASE_TABLES. Add a "
                    f"CREATE TABLE migration ordered before this view, or "
                    f"allowlist the cross-source base table with its owning "
                    f"migration path."
                )

        # Record tables AND views created by this file for later files.
        for tbl_match in _CREATE_TABLE_RE.finditer(clean):
            created.add(tbl_match.group("name").lower())
        for view_match in _CREATE_VIEW_RE.finditer(clean):
            created.add(view_match.group("name").lower())
    return violations


def _discover_node_migration_dirs(repo_root: pathlib.Path) -> list[pathlib.Path]:
    nodes_root = repo_root / "src" / "omnimarket" / "nodes"
    if not nodes_root.is_dir():
        return []
    dirs: list[pathlib.Path] = []
    for node_dir in sorted(nodes_root.iterdir()):
        migrations = node_dir / "migrations"
        if migrations.is_dir() and any(migrations.glob("*.sql")):
            dirs.append(migrations)
    return dirs


def _resolve_repo_root() -> pathlib.Path:
    start = pathlib.Path(__file__).resolve()
    for candidate in start.parents:
        if (candidate / "pyproject.toml").exists() and (
            candidate / "src" / "omnimarket"
        ).is_dir():
            return candidate
    return pathlib.Path.cwd()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--repo-root",
        type=pathlib.Path,
        default=None,
        help="omnimarket repo root (default: auto-resolve from this script).",
    )
    args = parser.parse_args(argv)
    repo_root = (args.repo_root or _resolve_repo_root()).resolve()

    all_violations: list[str] = []
    for migrations_dir in _discover_node_migration_dirs(repo_root):
        all_violations.extend(_check_node(migrations_dir, repo_root))

    if all_violations:
        print(
            "Node migration base-table-before-view gate FAILED "
            f"({len(all_violations)} violation(s)):",
            file=sys.stderr,
        )
        for violation in all_violations:
            print(f"  - {violation}", file=sys.stderr)
        return 1

    print("Node migration base-table-before-view gate: clean.")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:  # pragma: no cover - defensive top-level guard
        print(
            f"check_migration_base_before_view: invocation error: {exc}",
            file=sys.stderr,
        )
        sys.exit(2)
