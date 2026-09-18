# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Refuse a tenant posture on a relation its own contract declares INTERNAL.

OMN-18774. A relation whose owning node contract declares
``schema: omninode_internal`` (or ``platform_catalog``) is written through
``InternalProjectionTableOperation`` / ``CatalogProjectionTableOperation``
(``omnibase_infra/src/omnibase_infra/runtime/auto_wiring/handler_wiring.py``),
which REFUSES a ``tenant_id`` key on the row and issues no
``set_config('app.tenant_id', ...)`` at all. A row-level-security policy
predicated on that GUC is therefore a predicate no declared writer can ever
satisfy: the write survives only while the connection happens to own the table
and ``relforcerowsecurity`` happens to be off.

The operator ruled the end state on 2026-09-14
(``docs/tracking/ROLLING_WORK_LEDGER.md:654``, OPERATOR-CONSENT,
``approved_by=operator``): relations classified ``OMNINODE_INTERNAL`` or
ambiguous are OUT OF SCOPE for tenant row-level security. They receive no
tenant stamping and no RLS.

## What this gate asserts

For every relation declared by a node contract under
``src/omnimarket/nodes/<node>/contract.yaml`` ``db_io.db_tables``, it replays
the node migration tree in application order and computes the relation's FINAL
posture. An internal- or catalog-declared relation FAILS when its final posture
carries any of:

* row-level security ENABLEd,
* a policy whose ``USING`` / ``WITH CHECK`` body reads a session GUC
  (``current_setting(...)``),
* a ``tenant_id`` column.

A TENANT-declared relation is never flagged for any of the three: that is the
positive control, and it is asserted as such by the gate's own test
(``tests/test_omn18774_internal_relation_tenant_posture_gate.py``).

## Why the FINAL posture and not the diff

A diff-scoped gate answers "did this change add the shape", which leaves every
already-landed instance invisible and needs a base ref to run at all. The final
posture answers "does the tree express the shape", which is the question AC4
asks (the enumeration is the deliverable) and the question AC5 asks (a new
migration that enables RLS on an internal relation changes the final posture
and is refused). One computation serves both, and it runs identically in
pre-commit and in CI with no base-ref plumbing.

## Scope, stated rather than implied

This reads omnimarket's own node migration tree, which
``omnibase_infra/scripts/sync-node-migrations.sh`` vendors 1:1 into
``docker/migrations/forward/nodes/``. A migration authored DIRECTLY in that
vendored tree, with no omnimarket source file, is not covered here — there is
one such file today (``node_projection_registration/0006_grant_omninode_
runtime_node_service_registry.sql``). That is a deliberate scope boundary:
this gate guards the repository that owns the contracts it joins against.
Extending it to the vendored tree needs the infra repo's ledger domain column
as its classification source and is a separate gate in a separate repository.
"""

from __future__ import annotations

import argparse
import re
import sys
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from omnibase_core.enums.enum_database_schema_domain import EnumDatabaseSchemaDomain

from omnimarket.projection.relation_domains import (
    RelationDeclaration,
    iter_contract_relation_declarations,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
NODES_DIR = REPO_ROOT / "src" / "omnimarket" / "nodes"

#: The domains for which a GUC-predicated tenant posture is refused.
#:
#: Read from the same loader the RUNTIME handlers resolve their stamp decision
#: through (``omnimarket.projection.relation_domains``), so the gate cannot
#: classify a relation one way while the writer classifies it the other.
REFUSED_DOMAINS: frozenset[EnumDatabaseSchemaDomain] = frozenset(
    {
        EnumDatabaseSchemaDomain.OMNINODE_INTERNAL,
        EnumDatabaseSchemaDomain.PLATFORM_CATALOG,
    }
)

_LINE_COMMENT = re.compile(r"--[^\n]*")
_BLOCK_COMMENT = re.compile(r"/\*.*?\*/", re.DOTALL)

_IDENT = r"(?:[A-Za-z_][A-Za-z0-9_$]*)"
_QUALIFIED = rf"(?:(?:{_IDENT}|\"{_IDENT}\")\.)?(?P<relation>{_IDENT}|\"{_IDENT}\")"

_ENABLE_RLS = re.compile(
    rf"\bALTER\s+TABLE\s+(?:IF\s+EXISTS\s+)?(?:ONLY\s+)?{_QUALIFIED}\s+ENABLE\s+ROW\s+LEVEL\s+SECURITY",
    re.IGNORECASE,
)
_DISABLE_RLS = re.compile(
    rf"\bALTER\s+TABLE\s+(?:IF\s+EXISTS\s+)?(?:ONLY\s+)?{_QUALIFIED}\s+DISABLE\s+ROW\s+LEVEL\s+SECURITY",
    re.IGNORECASE,
)
_CREATE_POLICY = re.compile(
    rf"\bCREATE\s+POLICY\s+(?P<policy>{_IDENT})\s+ON\s+{_QUALIFIED}(?P<body>.*?);",
    re.IGNORECASE | re.DOTALL,
)
_DROP_POLICY = re.compile(
    rf"\bDROP\s+POLICY\s+(?:IF\s+EXISTS\s+)?(?P<policy>{_IDENT})\s+ON\s+{_QUALIFIED}",
    re.IGNORECASE,
)
_ADD_TENANT_COLUMN = re.compile(
    rf"\bALTER\s+TABLE\s+(?:IF\s+EXISTS\s+)?(?:ONLY\s+)?{_QUALIFIED}\s+ADD\s+COLUMN\s+(?:IF\s+NOT\s+EXISTS\s+)?tenant_id\b",
    re.IGNORECASE,
)
_DROP_TENANT_COLUMN = re.compile(
    rf"\bALTER\s+TABLE\s+(?:IF\s+EXISTS\s+)?(?:ONLY\s+)?{_QUALIFIED}\s+DROP\s+COLUMN\s+(?:IF\s+EXISTS\s+)?tenant_id\b",
    re.IGNORECASE,
)
_CREATE_TABLE_TENANT_COLUMN = re.compile(
    rf"\bCREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?{_QUALIFIED}\s*\((?P<body>.*?)\)\s*;",
    re.IGNORECASE | re.DOTALL,
)
_GUC_READ = re.compile(r"\bcurrent_setting\s*\(", re.IGNORECASE)


def strip_sql_comments(sql: str) -> str:
    """Remove ``--`` and ``/* */`` comments.

    Load-bearing, not cosmetic: several migrations in this tree DESCRIBE the
    statements they deliberately do not issue. ``node_projection_registration/
    0004_node_service_registry_no_force_rls.sql`` says in prose that it is
    "narrower than DISABLE ROW LEVEL SECURITY or DROP POLICY", and a scan over
    raw bytes would read that sentence as both statements and report the
    relation clean.
    """
    return _LINE_COMMENT.sub(" ", _BLOCK_COMMENT.sub(" ", sql))


def _unquote(identifier: str) -> str:
    return identifier.strip('"')


@dataclass
class RelationPosture:
    """The end state one relation's migration stream expresses."""

    relation: str
    rls_enabled: bool = False
    guc_policies: dict[str, str] = field(default_factory=dict)
    tenant_id_column: bool = False
    sources: list[str] = field(default_factory=list)

    def carries_tenant_posture(self) -> bool:
        return bool(self.rls_enabled or self.guc_policies or self.tenant_id_column)


@dataclass(frozen=True)
class Finding:
    relation: str
    node: str
    domain: str
    reasons: tuple[str, ...]
    sources: tuple[str, ...]

    def render(self) -> str:
        return (
            f"  {self.relation} (declared {self.domain} by {self.node}): "
            + "; ".join(self.reasons)
            + f"\n    migrations: {', '.join(self.sources)}"
        )


def load_declarations(
    nodes_dir: Path,
) -> tuple[dict[str, RelationDeclaration], list[str]]:
    """Map relation name -> declaration, plus the names declared inconsistently.

    A relation declared by two contracts under two different domains is
    reported as unresolved rather than resolved to either: picking one would be
    inventing the classification the gate exists to read. The runtime loader
    RAISES on that case; here it is collected so the gate can report every
    unresolved relation in one run instead of stopping at the first.
    """
    seen: dict[str, list[RelationDeclaration]] = {}
    for declaration in iter_contract_relation_declarations(nodes_dir):
        seen.setdefault(declaration.relation, []).append(declaration)

    resolved: dict[str, RelationDeclaration] = {}
    conflicted: list[str] = []
    for name, declarations in seen.items():
        domains = {declaration.domain for declaration in declarations}
        if len(domains) > 1:
            conflicted.append(
                f"{name}: declared {sorted(domain.value for domain in domains)} "
                f"by {sorted({declaration.node for declaration in declarations})}"
            )
            continue
        resolved[name] = declarations[0]
    return resolved, sorted(conflicted)


def replay_migrations(paths: Iterable[Path]) -> dict[str, RelationPosture]:
    """Compute each relation's final posture by replaying the tree in order."""
    postures: dict[str, RelationPosture] = {}

    def posture(relation: str, source: str) -> RelationPosture:
        entry = postures.setdefault(relation, RelationPosture(relation=relation))
        if source not in entry.sources:
            entry.sources.append(source)
        return entry

    for path in paths:
        source = f"{path.parent.parent.name}/{path.name}"
        sql = strip_sql_comments(path.read_text(encoding="utf-8"))
        events: list[tuple[int, str, re.Match[str]]] = []
        for kind, pattern in (
            ("enable", _ENABLE_RLS),
            ("disable", _DISABLE_RLS),
            ("create_policy", _CREATE_POLICY),
            ("drop_policy", _DROP_POLICY),
            ("add_tenant", _ADD_TENANT_COLUMN),
            ("drop_tenant", _DROP_TENANT_COLUMN),
            ("create_table", _CREATE_TABLE_TENANT_COLUMN),
        ):
            events.extend(
                (match.start(), kind, match) for match in pattern.finditer(sql)
            )
        for _, kind, match in sorted(events, key=lambda item: item[0]):
            relation = _unquote(match.group("relation"))
            if kind == "enable":
                posture(relation, source).rls_enabled = True
            elif kind == "disable":
                posture(relation, source).rls_enabled = False
            elif kind == "create_policy":
                if _GUC_READ.search(match.group("body")):
                    posture(relation, source).guc_policies[match.group("policy")] = (
                        source
                    )
                else:
                    posture(relation, source).guc_policies.pop(
                        match.group("policy"), None
                    )
            elif kind == "drop_policy":
                posture(relation, source).guc_policies.pop(match.group("policy"), None)
            elif kind == "add_tenant":
                posture(relation, source).tenant_id_column = True
            elif kind == "drop_tenant":
                posture(relation, source).tenant_id_column = False
            elif kind == "create_table" and re.search(
                r"(^|,)\s*tenant_id\b", match.group("body"), re.IGNORECASE
            ):
                posture(relation, source).tenant_id_column = True
    return postures


def node_migration_paths(nodes_dir: Path) -> list[Path]:
    """Every node migration, in the order the forward runner applies them."""
    return sorted(
        nodes_dir.glob("*/migrations/*.sql"),
        key=lambda path: (path.parent.parent.name, path.name),
    )


def evaluate(
    nodes_dir: Path,
) -> tuple[list[Finding], list[str], dict[str, RelationDeclaration]]:
    declarations, conflicted = load_declarations(nodes_dir)
    postures = replay_migrations(node_migration_paths(nodes_dir))
    findings: list[Finding] = []
    for relation, declaration in sorted(declarations.items()):
        if declaration.domain not in REFUSED_DOMAINS:
            continue
        posture = postures.get(relation)
        if posture is None or not posture.carries_tenant_posture():
            continue
        reasons: list[str] = []
        if posture.rls_enabled:
            reasons.append("row-level security is ENABLEd")
        for policy, source in sorted(posture.guc_policies.items()):
            reasons.append(f"policy {policy!r} reads a session GUC (from {source})")
        if posture.tenant_id_column:
            reasons.append("carries a tenant_id column")
        findings.append(
            Finding(
                relation=relation,
                node=declaration.node,
                domain=declaration.domain.value,
                reasons=tuple(reasons),
                sources=tuple(posture.sources),
            )
        )
    return findings, conflicted, declarations


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--nodes-dir",
        type=Path,
        default=NODES_DIR,
        help="node tree to evaluate (default: this repository's own)",
    )
    parser.add_argument(
        "filenames",
        nargs="*",
        help="ignored; accepted so pre-commit may pass changed paths",
    )
    args = parser.parse_args(argv)

    findings, conflicted, declarations = evaluate(args.nodes_dir)

    if conflicted:
        print(
            "check_internal_relation_tenant_posture: relations declared under "
            "more than one security domain -- classify them before this gate "
            "can judge their posture:",
            file=sys.stderr,
        )
        for line in conflicted:
            print(f"  {line}", file=sys.stderr)

    if findings:
        print(
            "check_internal_relation_tenant_posture: REFUSED -- "
            f"{len(findings)} relation(s) carry a tenant posture their own "
            "contract forbids (OMN-18774; operator ruling "
            "docs/tracking/ROLLING_WORK_LEDGER.md:654).",
            file=sys.stderr,
        )
        for finding in findings:
            print(finding.render(), file=sys.stderr)
        print(
            "\nAn OMNINODE_INTERNAL or PLATFORM_CATALOG relation receives no "
            "tenant stamping and no row-level security. Either drop the "
            "posture in a new migration, or re-classify the relation in its "
            "owning contract -- which for registry and orchestration state "
            "reverses the 2026-08-02 domain ruling and needs the operator.",
            file=sys.stderr,
        )
        return 1

    if conflicted:
        return 1

    internal = sum(
        1 for decl in declarations.values() if decl.domain in REFUSED_DOMAINS
    )
    print(
        "check_internal_relation_tenant_posture: OK -- "
        f"{internal} internal/catalog-declared relation(s) carry no "
        f"GUC-predicated tenant posture ({len(declarations)} declared in total)."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
