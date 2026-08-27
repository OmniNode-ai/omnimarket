#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-15797 AC3 rule 2 -- every Postgres statement against an RLS-covered
relation must carry the tenant GUC seam.

The defect class this closes
----------------------------
Postgres row-level security fails OPEN *from the caller's point of view*. With
``app.tenant_id`` unset, ``USING (tenant_id = current_setting('app.tenant_id',
true))`` evaluates to NULL, so an RLS-covered ``SELECT`` returns ZERO ROWS and
raises nothing. A caller cannot tell that answer apart from "the table is
empty". OMN-15797's original defect was exactly this: 12 tables, HTTP 200,
``row_count: 0``, no error anywhere.

OMN-15801 (the sibling gate) enforces WHO may open a database connection at
all. That is necessary but not sufficient, and this gate is the complement it
names: it does not catch a surface that is legitimately allowed to touch the
database and simply forgets the tenant seam on one statement. The live proof
that manual discovery is not enough is OMN-16092 --
``projection/postgres_read_database.py`` read the RLS-covered
``context_roi_scores`` from inside the runtime with zero tenant handling, and
its fail-open consumer degraded to static routing with no signal. It was found
by a person reading code, months later.

What is checked
---------------
A module is IN SCOPE when it both

  1. imports a PostgreSQL driver (``asyncpg`` / ``psycopg2`` / ``psycopg`` /
     ``aiopg``), including a function-local import -- the evasion shape used by
     ``probe_db_row_counts`` and ``handler_retention_cleanup`` -- and
  2. issues statements (``execute``/``executemany``/``fetch``/``fetchrow``/
     ``fetchval``/``fetchall``/``fetchmany``/``copy_*``).

An in-scope module FAILS when it names an RLS-covered relation in any string
constant without also referencing the tenant seam: the ``TENANT_GUC`` constant
AND at least one resolver from ``omnimarket.projection.tenant_isolation``.

The RLS relation set is DERIVED, not hand-listed: every
``ALTER TABLE <t> ENABLE ROW LEVEL SECURITY`` in this repo's own migrations.
A new RLS migration therefore widens this gate automatically, on the same
commit that adds the policy -- a hand-maintained list would have gone stale on
exactly the migration that mattered.

Why no allowlist
----------------
There is no per-line annotation and no registry escape hatch (contrast
OMN-15801, whose scope is broad enough that a sanctioned-surface registry is
the only workable shape). Setting the GUC is never harmful: a superuser/owner
connection bypasses RLS and is unaffected by it, and a non-superuser
connection goes from silently seeing nothing to seeing its tenant's rows. So
"this surface legitimately cannot set it" has no instance today, and inventing
the bypass before it has one is how gates decay (Operating Rule 10).

Usage
-----
    check_rls_read_tenant_seam.py                 # scan src/omnimarket
    check_rls_read_tenant_seam.py --root PATH
    check_rls_read_tenant_seam.py --selftest      # prove RED and GREEN

Exit codes: 0 = clean; 1 = at least one unseamed RLS statement surface;
2 = usage/input error (fail-closed).

SYNC: ci.yml job `lint`, step "RLS tenant-seam gate (OMN-15797)" + pre-commit
      hook 'rls-read-tenant-seam'.
"""

from __future__ import annotations

import argparse
import ast
import re
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

# PostgreSQL drivers only. sqlite3/aiosqlite are deliberately absent: SQLite
# has no row-level security, so a module reading a same-named table through
# omnimarket.projection.sqlite_database is not exposed to this defect class.
POSTGRES_DRIVERS: frozenset[str] = frozenset(
    {"asyncpg", "psycopg2", "psycopg", "aiopg"}
)

STATEMENT_METHODS: frozenset[str] = frozenset(
    {
        "execute",
        "executemany",
        "fetch",
        "fetchrow",
        "fetchval",
        "fetchall",
        "fetchmany",
        "copy_from_query",
        "copy_to_table",
    }
)

# The seam: the GUC constant plus any of the module's resolvers. Both halves
# are required -- a module that resolves a tenant but never sets the GUC has
# done nothing to the RLS predicate, and a module that sets a GUC to a value
# resolved nowhere is setting it to a guess.
TENANT_GUC_SYMBOL = "TENANT_GUC"
TENANT_RESOLVERS: frozenset[str] = frozenset(
    {
        "resolve_rls_read_tenant",
        "resolve_read_tenant",
        "resolve_write_tenant",
        "resolve_serving_tenant",
        "house_tenant_write_stamp",
    }
)

_ENABLE_RLS = re.compile(
    r"ALTER\s+TABLE\s+(?:IF\s+EXISTS\s+)?([A-Za-z0-9_.\"]+)\s+"
    r"ENABLE\s+ROW\s+LEVEL\s+SECURITY",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class Finding:
    """One in-scope module naming an RLS relation with no tenant seam."""

    path: Path
    line: int
    relations: tuple[str, ...]

    def render(self, root: Path) -> str:
        try:
            shown = self.path.relative_to(root)
        except ValueError:
            shown = self.path
        return (
            f"{shown}:{self.line}: rls-read-tenant-seam: issues Postgres "
            f"statements naming RLS-covered relation(s) "
            f"{', '.join(self.relations)} but never sets {TENANT_GUC_SYMBOL}. "
            "With app.tenant_id unset the RLS predicate is NULL and the "
            "statement silently matches ZERO rows (OMN-15797). Resolve a "
            "tenant with one of "
            f"{', '.join(sorted(TENANT_RESOLVERS))} from "
            "omnimarket.projection.tenant_isolation and issue "
            "SELECT set_config(TENANT_GUC, <tenant>, true) inside the same "
            "transaction as the statement -- see "
            "src/omnimarket/projection/postgres_read_database.py for the "
            "reference shape."
        )


def rls_relations(root: Path) -> frozenset[str]:
    """Every relation this repo's own migrations put under RLS."""
    found: set[str] = set()
    for sql_path in root.rglob("migrations/*.sql"):
        for match in _ENABLE_RLS.finditer(sql_path.read_text(errors="ignore")):
            found.add(match.group(1).strip('"').split(".")[-1])
    return frozenset(found)


@dataclass
class _ModuleFacts:
    drivers: set[str]
    statement_lines: list[int]
    seam_guc: bool
    seam_resolver: bool
    relation_hits: dict[str, int]


def _scan_module(tree: ast.AST, relations: frozenset[str]) -> _ModuleFacts:
    facts = _ModuleFacts(
        drivers=set(),
        statement_lines=[],
        seam_guc=False,
        seam_resolver=False,
        relation_hits={},
    )
    word = {rel: re.compile(rf"\b{re.escape(rel)}\b") for rel in relations}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                base = alias.name.split(".")[0]
                if base in POSTGRES_DRIVERS:
                    facts.drivers.add(base)
        elif isinstance(node, ast.ImportFrom):
            base = (node.module or "").split(".")[0]
            if base in POSTGRES_DRIVERS:
                facts.drivers.add(base)
        elif isinstance(node, ast.Attribute):
            if node.attr in STATEMENT_METHODS:
                facts.statement_lines.append(node.lineno)
            elif node.attr == TENANT_GUC_SYMBOL:
                facts.seam_guc = True
            elif node.attr in TENANT_RESOLVERS:
                facts.seam_resolver = True
        elif isinstance(node, ast.Name):
            if node.id == TENANT_GUC_SYMBOL:
                facts.seam_guc = True
            elif node.id in TENANT_RESOLVERS:
                facts.seam_resolver = True
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            for rel, pattern in word.items():
                if rel not in facts.relation_hits and pattern.search(node.value):
                    facts.relation_hits[rel] = node.lineno
    return facts


def scan(root: Path) -> list[Finding]:
    """Return one finding per in-scope module lacking the tenant seam."""
    relations = rls_relations(root)
    if not relations:
        print(
            "rls-read-tenant-seam: no ENABLE ROW LEVEL SECURITY migration "
            f"found under {root} -- refusing to report a clean scan from an "
            "empty relation set (fail-closed)",
            file=sys.stderr,
        )
        raise SystemExit(2)

    findings: list[Finding] = []
    for py_path in sorted(root.rglob("*.py")):
        source = py_path.read_text(errors="ignore")
        try:
            tree = ast.parse(source)
        except SyntaxError:
            continue
        facts = _scan_module(tree, relations)
        if not (facts.drivers and facts.statement_lines):
            continue
        if not facts.relation_hits:
            continue
        if facts.seam_guc and facts.seam_resolver:
            continue
        findings.append(
            Finding(
                path=py_path,
                line=min(facts.relation_hits.values()),
                relations=tuple(sorted(facts.relation_hits)),
            )
        )
    return findings


# ---------------------------------------------------------------------------
# Selftest -- proves this gate's own RED and GREEN without git or a database
# ---------------------------------------------------------------------------

_MIGRATION = "ALTER TABLE widget_rows ENABLE ROW LEVEL SECURITY;\n"

_UNSEAMED = """
import asyncpg


async def read(conn):
    return await conn.fetch("SELECT * FROM widget_rows")
"""

_SEAMED = """
import asyncpg

from omnimarket.projection.tenant_isolation import (
    TENANT_GUC,
    resolve_rls_read_tenant,
)


async def read(conn):
    tenant = resolve_rls_read_tenant(None, table="widget_rows")
    async with conn.transaction():
        await conn.execute("SELECT set_config($1, $2, true)", TENANT_GUC, tenant)
        return await conn.fetch("SELECT * FROM widget_rows")
"""

_NO_DRIVER = '''
WIDGET_TABLE = "widget_rows"


def describe():
    """Names the relation but issues no statement -- not in scope."""
    return WIDGET_TABLE
'''

_UNRELATED_TABLE = """
import asyncpg


async def read(conn):
    return await conn.fetch("SELECT * FROM not_covered_by_rls")
"""


def _selftest() -> int:
    cases: list[tuple[str, str, bool]] = [
        ("unseamed.py", _UNSEAMED, True),
        ("seamed.py", _SEAMED, False),
        ("no_driver.py", _NO_DRIVER, False),
        ("unrelated_table.py", _UNRELATED_TABLE, False),
    ]
    failures: list[str] = []
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "migrations").mkdir()
        (root / "migrations" / "0001_rls.sql").write_text(_MIGRATION)
        for name, body, _ in cases:
            (root / name).write_text(body)

        assert rls_relations(root) == frozenset({"widget_rows"})
        flagged = {finding.path.name for finding in scan(root)}
        for name, _body, should_flag in cases:
            if should_flag and name not in flagged:
                failures.append(f"{name}: expected a finding, got none (gate is blind)")
            if not should_flag and name in flagged:
                failures.append(f"{name}: unexpected finding (gate is over-broad)")

    for failure in failures:
        print(f"rls-read-tenant-seam selftest FAILED: {failure}", file=sys.stderr)
    if failures:
        return 1
    print("rls-read-tenant-seam selftest: RED and GREEN both proven (4 cases)")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--root",
        default="src/omnimarket",
        help="tree to scan (default: src/omnimarket)",
    )
    parser.add_argument(
        "--selftest",
        action="store_true",
        help="prove the gate's own RED/GREEN behaviour against a temp tree",
    )
    args = parser.parse_args(argv)

    if args.selftest:
        return _selftest()

    root = Path(args.root)
    if not root.is_dir():
        print(f"rls-read-tenant-seam: {root} is not a directory", file=sys.stderr)
        return 2

    findings = scan(root)
    for finding in findings:
        print(finding.render(root), file=sys.stderr)
    if findings:
        print(
            f"rls-read-tenant-seam: {len(findings)} unseamed RLS statement "
            "surface(s) (OMN-15797 AC3)",
            file=sys.stderr,
        )
        return 1
    print("rls-read-tenant-seam: clean")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
