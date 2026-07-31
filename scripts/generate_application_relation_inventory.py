#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Build the source-derived OMN-15423 application-relation inventory.

The ownership source of truth stays distributed: tables are declared in each
node's ``contract.yaml -> db_io.db_tables`` and DDL stays beside the owning
node.  This script projects those sources into one reviewable evidence file;
the generated file is not an ownership allowlist.

Only repository evidence is used.  In particular, this script never opens a
database connection and never claims that a checked-in DSN consumer was active
during the required full-day observation window.
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

import yaml
from omnibase_core.models.contracts.subcontracts.model_db_table_declaration import (
    ModelDbTableDeclaration,
)
from pydantic import ValidationError

REPO_ROOT = Path(__file__).resolve().parents[1]
NODES_ROOT = REPO_ROOT / "src" / "omnimarket" / "nodes"
DEFAULT_OUTPUT = REPO_ROOT / "docs" / "evidence" / "OMN-15423-relation-inventory.json"
SERVICE_MANIFEST = REPO_ROOT / "scripts" / "application-relation-ownership.yaml"
SERVICE_SOURCE = REPO_ROOT / "scripts" / "run-projection-migrations.py"
SERVICE_OWNER = "service:omnimarket_projection_migration_runner"
RETAINED_LIVE_CENSUS_SOURCE = (
    "omni_home/docs/plans/2026-07-29-two-database-tenant-vs-internal-split-plan.md"
)

DOMAIN_BY_SCHEMA = {
    "public": "PUBLIC",
    "tenant": "TENANT",
    "omninode_internal": "OMNINODE_INTERNAL",
    "platform_catalog": "PLATFORM_CATALOG",
}

# These are blockers, not ownership exceptions.  They are named by the
# approved plan and remain absent from the classified owner set until semantic
# evidence supplies a topology-resolvable schema.
CROSS_REPO_BLOCKERS: tuple[dict[str, Any], ...] = (
    {
        "name": "delegation_workflow_state",
        "kind": "table",
        "current_database": "omnibase_infra",
        "authoritative_sources": [
            "omnibase_infra/docker/migrations/forward/090_create_delegation_workflow_state.sql",
            "omnibase_infra/docker/migrations/forward/093_add_delegation_workflow_state_outbox_columns.sql",
        ],
        "reason": "producer, consumers, and customer-ownership semantics are unresolved",
    },
    {
        "name": "event_bus_events",
        "kind": "table",
        "current_database": "omnidash_analytics",
        "authoritative_sources": [],
        "reason": "observed in the retained live census but no authoritative repository DDL was found",
    },
    {
        "name": "schema_migrations",
        "kind": "table",
        "current_database": "omnidash_analytics",
        "authoritative_sources": [],
        "reason": (
            "retained live evidence identifies the omnidash filename ledger, but "
            "no authoritative repository DDL or owner declaration was found"
        ),
    },
)

_IDENTIFIER = r'(?:(?:"[^"]+"|[A-Za-z_][\w$]*)\.)?(?:"[^"]+"|[A-Za-z_][\w$]*)'
_CREATE_RE = re.compile(
    rf"\bCREATE\s+(?:OR\s+REPLACE\s+)?"
    rf"(?P<kind>MATERIALIZED\s+VIEW|TABLE|VIEW|FUNCTION|PROCEDURE|SEQUENCE|EXTENSION)\s+"
    rf"(?:IF\s+NOT\s+EXISTS\s+)?(?P<name>{_IDENTIFIER})",
    re.IGNORECASE,
)
_INDEX_RE = re.compile(
    rf"\bCREATE\s+(?P<unique>UNIQUE\s+)?INDEX\s+(?:CONCURRENTLY\s+)?"
    rf"(?:IF\s+NOT\s+EXISTS\s+)?(?P<index>[A-Za-z_][\w$]*)\s+"
    rf"ON\s+(?:ONLY\s+)?(?P<table>{_IDENTIFIER})",
    re.IGNORECASE,
)
_GRANT_RE = re.compile(
    rf"\bGRANT\s+(?P<privileges>[A-Z_,\s]+?)\s+ON\s+"
    rf"(?:TABLE\s+)?(?P<object>{_IDENTIFIER})\s+TO\s+"
    rf"(?P<principal>[A-Za-z_][\w$]*)",
    re.IGNORECASE,
)
_REFERENCE_RE = re.compile(rf"\bREFERENCES\s+(?P<name>{_IDENTIFIER})", re.IGNORECASE)
_FOREIGN_KEY_RE = re.compile(
    rf"\bFOREIGN\s+KEY\s*\((?P<local>[^)]+)\)\s*"
    rf"REFERENCES\s+(?P<table>{_IDENTIFIER})"
    rf"(?:\s*\((?P<remote>[^)]+)\))?",
    re.IGNORECASE,
)
_INLINE_FOREIGN_KEY_RE = re.compile(
    rf"^\s*(?!(?:CONSTRAINT|UNIQUE|PRIMARY|FOREIGN)\b)"
    rf"(?P<local>\"?[A-Za-z_][\w$]*\"?)\s+[^,\n]*?"
    rf"\bREFERENCES\s+(?P<table>{_IDENTIFIER})"
    rf"(?:\s*\((?P<remote>[^)]+)\))?",
    re.IGNORECASE | re.MULTILINE,
)
_UNIQUE_RE = re.compile(
    r"\bUNIQUE(?:\s+NULLS\s+NOT\s+DISTINCT)?\s*\(([^)]+)\)",
    re.IGNORECASE,
)
_INLINE_PRIMARY_KEY_RE = re.compile(
    r"^\s*(?!(?:CONSTRAINT|UNIQUE|PRIMARY|FOREIGN)\b)"
    r'(?P<column>"?[A-Za-z_][\w$]*"?)\s+[^,\n]*?\bPRIMARY\s+KEY\b',
    re.IGNORECASE | re.MULTILINE,
)
_INLINE_UNIQUE_RE = re.compile(
    r"^\s*(?!(?:CONSTRAINT|UNIQUE|PRIMARY|FOREIGN)\b)"
    r'(?P<column>"?[A-Za-z_][\w$]*"?)\s+[^,\n]*?\bUNIQUE\b',
    re.IGNORECASE | re.MULTILINE,
)
_SERIAL_RE = re.compile(
    r'(?P<column>"?[A-Za-z_][\w$]*"?)\s+(?P<type>BIGSERIAL|SMALLSERIAL|SERIAL)\b',
    re.IGNORECASE,
)


def _unquote(value: str) -> str:
    """Return the unqualified, unquoted PostgreSQL object name."""
    return value.rsplit(".", 1)[-1].replace('"', "").lower()


def _schema(value: str) -> str:
    """Return an explicit schema or the current legacy default, ``public``."""
    if "." not in value:
        return "public"
    return value.rsplit(".", 1)[0].replace('"', "").lower()


def _mask_sql(text: str) -> str:
    """Mask comments and literal bodies while preserving offsets and newlines."""
    output: list[str] = []
    index = 0
    state = "normal"
    dollar_tag = ""
    while index < len(text):
        if state == "normal":
            if text.startswith("--", index):
                output.extend("  ")
                index += 2
                state = "line_comment"
                continue
            if text.startswith("/*", index):
                output.extend("  ")
                index += 2
                state = "block_comment"
                continue
            if text[index] == "'":
                output.append(" ")
                index += 1
                state = "single_quote"
                continue
            if text[index] == "$":
                match = re.match(r"\$[A-Za-z_][A-Za-z0-9_]*\$|\$\$", text[index:])
                if match:
                    dollar_tag = match.group(0)
                    output.extend(" " * len(dollar_tag))
                    index += len(dollar_tag)
                    state = "dollar_quote"
                    continue
            output.append(text[index])
            index += 1
            continue

        if state == "line_comment":
            if text[index] == "\n":
                output.append("\n")
                state = "normal"
            else:
                output.append(" ")
            index += 1
            continue

        if state == "block_comment":
            if text.startswith("*/", index):
                output.extend("  ")
                index += 2
                state = "normal"
            else:
                output.append("\n" if text[index] == "\n" else " ")
                index += 1
            continue

        if state == "single_quote":
            if text.startswith("''", index):
                output.extend("  ")
                index += 2
            elif text[index] == "'":
                output.append(" ")
                index += 1
                state = "normal"
            else:
                output.append("\n" if text[index] == "\n" else " ")
                index += 1
            continue

        if text.startswith(dollar_tag, index):
            output.extend(" " * len(dollar_tag))
            index += len(dollar_tag)
            state = "normal"
        else:
            output.append("\n" if text[index] == "\n" else " ")
            index += 1

    return "".join(output)


def _statement(masked: str, start: int) -> str:
    """Return one masked SQL statement starting at ``start``."""
    end = masked.find(";", start)
    return masked[start:] if end < 0 else masked[start : end + 1]


def _create_table_body(masked: str, match: re.Match[str]) -> str:
    """Return the balanced CREATE TABLE column/constraint body."""
    start = masked.find("(", match.end())
    if start < 0:
        return ""
    depth = 0
    for index in range(start, len(masked)):
        character = masked[index]
        if character == "(":
            depth += 1
        elif character == ")":
            depth -= 1
            if depth == 0:
                return masked[start + 1 : index]
    return ""


def _service_migration_sql() -> str:
    """Extract the repository-owned ledger DDL without executing the runner."""
    module = ast.parse(SERVICE_SOURCE.read_text(encoding="utf-8"))
    for statement in module.body:
        if not isinstance(statement, ast.Assign):
            continue
        if not any(
            isinstance(target, ast.Name) and target.id == "CREATE_MIGRATIONS_TABLE"
            for target in statement.targets
        ):
            continue
        if isinstance(statement.value, ast.Constant) and isinstance(
            statement.value.value, str
        ):
            return statement.value.value
    raise ValueError(
        f"{SERVICE_SOURCE} must define literal CREATE_MIGRATIONS_TABLE SQL"
    )


def _load_contracts() -> tuple[
    dict[str, list[dict[str, Any]]], dict[str, dict[str, Any]]
]:
    declarations: dict[str, list[dict[str, Any]]] = defaultdict(list)
    contracts: dict[str, dict[str, Any]] = {}
    required = {"name", "database_ref", "schema", "migration", "access", "role"}

    def validate_declaration(path: Path, declaration: Any) -> dict[str, Any]:
        display_path = (
            path.relative_to(REPO_ROOT) if path.is_relative_to(REPO_ROOT) else path
        )
        if not isinstance(declaration, dict):
            raise ValueError(
                f"{display_path} db_io.db_tables entries must be "
                f"mappings, got {type(declaration).__name__}"
            )
        actual = set(declaration)
        if actual != required:
            raise ValueError(
                f"{display_path} {declaration.get('name')!r} "
                f"must use exactly {sorted(required)} and must not use the retired "
                f"database key; got {sorted(actual)}"
            )
        try:
            typed = ModelDbTableDeclaration.model_validate(declaration)
        except ValidationError as exc:
            raise ValueError(
                f"{display_path} {declaration.get('name')!r} has "
                f"an invalid typed database_ref/schema location: {exc}"
            ) from exc
        return typed.model_dump(mode="python")

    for path in sorted(NODES_ROOT.glob("*/contract.yaml")):
        payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        node = path.parent.name
        contracts[node] = payload
        for raw_declaration in (payload.get("db_io") or {}).get("db_tables") or []:
            declaration = validate_declaration(path, raw_declaration)
            if declaration["database_ref"] != "application":
                continue
            item = dict(declaration)
            item["node"] = node
            item["contract"] = str(path.relative_to(REPO_ROOT))
            declarations[str(declaration["name"]).lower()].append(item)

    service_payload = yaml.safe_load(SERVICE_MANIFEST.read_text(encoding="utf-8")) or {}
    if service_payload.get("owner_declaration") != SERVICE_OWNER:
        raise ValueError(f"{SERVICE_MANIFEST} must declare owner {SERVICE_OWNER!r}")
    for raw_declaration in (service_payload.get("db_io") or {}).get("db_tables") or []:
        declaration = validate_declaration(SERVICE_MANIFEST, raw_declaration)
        if declaration["database_ref"] != "application":
            continue
        item = dict(declaration)
        item["node"] = SERVICE_OWNER
        item["contract"] = str(SERVICE_MANIFEST.relative_to(REPO_ROOT))
        declarations[str(declaration["name"]).lower()].append(item)
    return declarations, contracts


def _source_readers(table_names: set[str]) -> dict[str, set[str]]:
    """Map table names to node packages that name them outside migration SQL."""
    readers: dict[str, set[str]] = defaultdict(set)
    token_re = re.compile(r"\b[A-Za-z_][A-Za-z0-9_]*\b")
    for path in NODES_ROOT.glob("*/**/*.py"):
        tokens = set(
            token_re.findall(path.read_text(encoding="utf-8", errors="replace"))
        )
        node = path.relative_to(NODES_ROOT).parts[0]
        for name in table_names & tokens:
            readers[name].add(node)
    return readers


def _target_schema(
    owner: str | None,
    dependencies: set[str],
    table_schemas: dict[str, str],
    declarations: dict[str, list[dict[str, Any]]],
) -> str:
    dependent_schemas = {
        table_schemas[name] for name in dependencies if name in table_schemas
    }
    if "tenant" in dependent_schemas:
        return "tenant"
    if "unresolved" in dependent_schemas:
        return "unresolved"
    if len(dependent_schemas) == 1:
        return next(iter(dependent_schemas))
    if owner:
        owned_schemas = {
            str(item["schema"])
            for items in declarations.values()
            for item in items
            if item["node"] == owner
        }
        if "tenant" in owned_schemas:
            return "tenant"
        if "omninode_internal" in owned_schemas:
            return "omninode_internal"
    return "unresolved"


def build_inventory() -> dict[str, Any]:
    """Return a deterministic source-only relation inventory."""
    declarations, _contracts = _load_contracts()
    sources: dict[tuple[str, str], set[str]] = defaultdict(set)
    owners: dict[tuple[str, str], set[str]] = defaultdict(set)
    current_schemas: dict[tuple[str, str], set[str]] = defaultdict(set)
    bodies: dict[tuple[str, str], list[str]] = defaultdict(list)
    statements: dict[tuple[str, str], list[str]] = defaultdict(list)
    masked_by_path: dict[Path, str] = {}

    migration_sources: list[tuple[Path, str, str | None]] = [
        (path, path.relative_to(NODES_ROOT).parts[0], None)
        for path in NODES_ROOT.glob("*/migrations/*.sql")
    ]
    migration_sources.append((SERVICE_SOURCE, SERVICE_OWNER, _service_migration_sql()))
    for path, node, sql_override in sorted(
        migration_sources, key=lambda item: str(item[0])
    ):
        relative = str(path.relative_to(REPO_ROOT))
        masked = _mask_sql(
            sql_override
            if sql_override is not None
            else path.read_text(encoding="utf-8")
        )
        masked_by_path[path] = masked
        for match in _CREATE_RE.finditer(masked):
            kind = match.group("kind").lower().replace(" ", "_")
            name = _unquote(match.group("name"))
            key = (kind, name)
            sources[key].add(relative)
            owners[key].add(node)
            current_schemas[key].add(_schema(match.group("name")))
            statement = _statement(masked, match.start())
            statements[key].append(statement)
            if kind == "table":
                bodies[key].append(_create_table_body(masked, match))

    created_tables = {name for kind, name in sources if kind == "table"}
    missing_declarations = sorted(created_tables - set(declarations))
    if missing_declarations:
        raise ValueError(
            "application sources create tables with no db_io declaration: "
            f"{missing_declarations}"
        )

    table_schemas: dict[str, str] = {}
    for name, items in declarations.items():
        schemas = {str(item["schema"]) for item in items}
        if len(schemas) != 1:
            raise ValueError(f"conflicting schemas for {name}: {sorted(schemas)}")
        table_schemas[name] = next(iter(schemas))

    readers_by_table = _source_readers(set(table_schemas))

    index_names: dict[str, set[str]] = defaultdict(set)
    unique_index_names: dict[str, set[str]] = defaultdict(set)
    grant_names: dict[str, set[str]] = defaultdict(set)
    for masked in masked_by_path.values():
        for match in _INDEX_RE.finditer(masked):
            table_name = _unquote(match.group("table"))
            index_name = match.group("index").lower()
            index_names[table_name].add(index_name)
            if match.group("unique"):
                unique_index_names[table_name].add(index_name)
        for match in _GRANT_RE.finditer(masked):
            privileges = " ".join(match.group("privileges").upper().split())
            grant_names[_unquote(match.group("object"))].add(
                f"{privileges} TO {match.group('principal').lower()}"
            )

    relation_rows: list[dict[str, Any]] = []
    table_rows: dict[str, dict[str, Any]] = {}
    all_table_names = sorted(set(declarations) | created_tables)
    for name in all_table_names:
        key = ("table", name)
        creation_owners = sorted(owners.get(key, set()))
        owner = creation_owners[0] if len(creation_owners) == 1 else None
        accessors = sorted({str(item["node"]) for item in declarations[name]})
        declared_readers = {
            str(item["node"])
            for item in declarations[name]
            if item["access"] in {"read", "read_write"}
        }
        writers = sorted(
            {
                str(item["node"])
                for item in declarations[name]
                if item["access"] in {"write", "read_write"}
            }
        )
        schema = table_schemas[name]
        body = "\n".join(bodies.get(key, []))
        statement_text = "\n".join(statements.get(key, []))
        constraints = sorted(
            set(re.findall(r"\bCONSTRAINT\s+([A-Za-z_][\w$]*)", body, re.IGNORECASE))
        )
        primary_keys = sorted(
            {
                " ".join(value.split())
                for value in re.findall(
                    r"PRIMARY\s+KEY\s*\(([^)]+)\)", body, re.IGNORECASE
                )
            }
            | {
                _unquote(match.group("column"))
                for match in _INLINE_PRIMARY_KEY_RE.finditer(body)
            }
        )
        unique_keys = sorted(
            {" ".join(value.split()) for value in _UNIQUE_RE.findall(body)}
            | {
                _unquote(match.group("column"))
                for match in _INLINE_UNIQUE_RE.finditer(body)
            }
        )
        foreign_keys = []
        for match in _FOREIGN_KEY_RE.finditer(body):
            table_name = _unquote(match.group("table"))
            remote = " ".join((match.group("remote") or "").split())
            reference = f"REFERENCES {table_name}"
            if remote:
                reference += f" ({remote})"
            local = " ".join((match.group("local") or "").split())
            foreign_keys.append(
                f"FOREIGN KEY ({local}) {reference}" if local else reference
            )
        for match in _INLINE_FOREIGN_KEY_RE.finditer(body):
            table_name = _unquote(match.group("table"))
            local = _unquote(match.group("local"))
            remote = " ".join((match.group("remote") or "").split())
            reference = f"REFERENCES {table_name}"
            if remote:
                reference += f" ({remote})"
            foreign_keys.append(f"FOREIGN KEY ({local}) {reference}")
        dependencies = sorted(
            {_unquote(match.group("name")) for match in _REFERENCE_RE.finditer(body)}
        )
        partitioning = [
            " ".join(value.split())
            for value in re.findall(
                r"\bPARTITION\s+BY\s+([^;]+)", statement_text, re.IGNORECASE
            )
        ]
        blocked_reasons: list[str] = []
        if owner is None:
            if not creation_owners:
                blocked_reasons.append("no authoritative CREATE TABLE migration found")
            else:
                blocked_reasons.append(f"multiple migration owners: {creation_owners}")
        if schema not in DOMAIN_BY_SCHEMA:
            blocked_reasons.append(
                f"schema {schema!r} does not resolve through deployment topology"
            )
        tenant_column_sources: list[str] = []
        tenant_column_dependencies: list[str] = []
        if schema == "omninode_internal":
            name_re = re.compile(rf"\b{re.escape(name)}\b", re.IGNORECASE)
            for path, masked in masked_by_path.items():
                if not name_re.search(masked) or not re.search(
                    r"\btenant_id\b", masked, re.IGNORECASE
                ):
                    continue
                tenant_column_sources.append(str(path.relative_to(REPO_ROOT)))
                for line in masked.splitlines():
                    if not re.search(r"\btenant_id\b", line, re.IGNORECASE):
                        continue
                    if re.search(
                        r"\b(PRIMARY\s+KEY|UNIQUE|FOREIGN\s+KEY|REFERENCES|INDEX|PARTITION)\b",
                        line,
                        re.IGNORECASE,
                    ):
                        tenant_column_dependencies.append(" ".join(line.split()))
        tenant_column_transform: dict[str, Any] | None = None
        if schema == "omninode_internal":
            tenant_column_transform = {
                "status": (
                    "source_dependency_inventory_complete_runtime_collision_scan_blocked"
                    if tenant_column_sources
                    else "not_applicable_no_source_tenant_id"
                ),
                "source_occurrences": sorted(set(tenant_column_sources)),
                "key_fk_index_partition_dependencies": sorted(
                    set(tenant_column_dependencies)
                ),
                "runtime_collision_scan": (
                    "blocked_live_database_access_not_authorized"
                    if tenant_column_sources
                    else "not_applicable"
                ),
            }
        row = {
            "name": name,
            "kind": "table",
            "current_schema": sorted(current_schemas.get(key, {"public"})),
            "target_schema": schema,
            "domain": DOMAIN_BY_SCHEMA.get(schema),
            "classification_status": "blocked" if blocked_reasons else "classified",
            "blocked_reasons": blocked_reasons,
            "owner_declaration": owner,
            "producer": owner,
            "accessor_nodes": accessors,
            "readers": sorted(
                (readers_by_table.get(name, set()) - ({owner} if owner else set()))
                | declared_readers
            ),
            "writers": writers,
            "migration_root": (
                "scripts"
                if owner and owner.startswith("service:")
                else "src/omnimarket/nodes"
            ),
            "migration_stream": (
                owner
                if owner and owner.startswith("service:")
                else f"node:{owner}"
                if owner
                else None
            ),
            "authoritative_sources": sorted(sources.get(key, set())),
            "contract_sources": sorted(
                str(item["contract"]) for item in declarations[name]
            ),
            "dependencies": dependencies,
            "dependent_objects": [],
            "keys": sorted(
                {f"PRIMARY KEY ({value})" for value in primary_keys}
                | {f"UNIQUE ({value})" for value in unique_keys}
                | {
                    f"UNIQUE INDEX {index}"
                    for index in unique_index_names.get(name, set())
                }
            ),
            "foreign_keys": sorted(set(foreign_keys)),
            "constraints": constraints,
            "indexes": sorted(index_names.get(name, set())),
            "partitioning": partitioning,
            "grants": sorted(grant_names.get(name, set())),
            "dsn_consumers": ["OMNIDASH_ANALYTICS_DB_URL"],
            "classification_evidence": (
                "service ownership manifest -> db_io.db_tables schema"
                if owner and owner.startswith("service:")
                else "contract.yaml -> db_io.db_tables schema"
            ),
            "internal_tenant_column_transform": tenant_column_transform,
        }
        table_rows[name] = row
        relation_rows.append(row)

    for (kind, name), relation_sources in sorted(sources.items()):
        if kind == "table":
            continue
        relation_owners = sorted(owners[(kind, name)])
        owner = relation_owners[0] if len(relation_owners) == 1 else None
        text = "\n".join(statements[(kind, name)])
        dependencies = {
            candidate
            for candidate in table_rows
            if re.search(rf"\b{re.escape(candidate)}\b", text, re.IGNORECASE)
        }
        if kind == "extension":
            schema = "platform_catalog"
            owner = "omnimarket_node_migration_stream"
        else:
            schema = _target_schema(owner, dependencies, table_schemas, declarations)
        blocked_reasons = []
        if owner is None and kind != "extension":
            blocked_reasons.append(
                f"multiple migration owners: {relation_owners}"
                if relation_owners
                else "no authoritative CREATE migration found"
            )
        if schema not in DOMAIN_BY_SCHEMA:
            blocked_reasons.append(
                f"schema {schema!r} does not resolve through deployment topology"
            )
        row = {
            "name": name,
            "kind": kind,
            "current_schema": sorted(current_schemas[(kind, name)]),
            "target_schema": schema,
            "domain": DOMAIN_BY_SCHEMA.get(schema),
            "classification_status": "blocked" if blocked_reasons else "classified",
            "blocked_reasons": blocked_reasons,
            "owner_declaration": owner,
            "producer": owner,
            "accessor_nodes": relation_owners,
            "readers": [],
            "writers": [],
            "migration_root": "src/omnimarket/nodes",
            "migration_stream": (
                "omnimarket_node_migrations"
                if kind == "extension"
                else f"node:{owner}"
                if owner
                else None
            ),
            "authoritative_sources": sorted(relation_sources),
            "contract_sources": [],
            "dependencies": sorted(dependencies),
            "dependent_objects": [],
            "keys": [],
            "foreign_keys": [],
            "constraints": [],
            "indexes": [],
            "partitioning": [],
            "grants": sorted(grant_names.get(name, set())),
            "dsn_consumers": ["OMNIDASH_ANALYTICS_DB_URL"],
            "classification_evidence": "owning node migration plus referenced table domains",
        }
        relation_rows.append(row)

        # SERIAL/BIGSERIAL create owned sequences even when SQL has no explicit
        # CREATE SEQUENCE statement.  Record those objects beside their tables.
    existing_sequences = {
        row["name"] for row in relation_rows if row["kind"] == "sequence"
    }
    implicit_sequences: list[dict[str, Any]] = []
    for name, table in table_rows.items():
        body = "\n".join(bodies.get(("table", name), []))
        for match in _SERIAL_RE.finditer(body):
            sequence_name = f"{name}_{_unquote(match.group('column'))}_seq"
            if sequence_name in existing_sequences:
                continue
            existing_sequences.add(sequence_name)
            implicit_sequences.append(
                {
                    "name": sequence_name,
                    "kind": "sequence",
                    "current_schema": table["current_schema"],
                    "target_schema": table["target_schema"],
                    "domain": table["domain"],
                    "classification_status": table["classification_status"],
                    "blocked_reasons": list(table["blocked_reasons"]),
                    "owner_declaration": table["owner_declaration"],
                    "producer": table["producer"],
                    "accessor_nodes": list(table["accessor_nodes"]),
                    "readers": [],
                    "writers": list(table["writers"]),
                    "migration_root": table["migration_root"],
                    "migration_stream": table["migration_stream"],
                    "authoritative_sources": list(table["authoritative_sources"]),
                    "contract_sources": list(table["contract_sources"]),
                    "dependencies": [name],
                    "dependent_objects": [f"table:{name}"],
                    "keys": [],
                    "foreign_keys": [],
                    "constraints": [],
                    "indexes": [],
                    "partitioning": [],
                    "grants": [],
                    "dsn_consumers": ["OMNIDASH_ANALYTICS_DB_URL"],
                    "classification_evidence": f"implicit {match.group('type').upper()} sequence for {name}",
                }
            )
    relation_rows.extend(implicit_sequences)

    by_name: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in relation_rows:
        by_name[str(row["name"])].append(row)
    for row in relation_rows:
        for dependency in row["dependencies"]:
            for target in by_name.get(dependency, []):
                target["dependent_objects"].append(f"{row['kind']}:{row['name']}")
    for row in relation_rows:
        row["dependent_objects"] = sorted(set(row["dependent_objects"]))

    relation_rows.sort(key=lambda row: (str(row["kind"]), str(row["name"])))
    blocked_relations = [
        {
            "name": row["name"],
            "kind": row["kind"],
            "current_database": "omnidash_analytics",
            "authoritative_sources": row["authoritative_sources"],
            "reason": "; ".join(row["blocked_reasons"]),
        }
        for row in relation_rows
        if row["classification_status"] == "blocked"
    ]
    blocked_relations.extend(dict(item) for item in CROSS_REPO_BLOCKERS)
    blocked_relations.sort(key=lambda item: str(item["name"]))

    counts: dict[str, int] = defaultdict(int)
    for row in relation_rows:
        counts[str(row["kind"])] += 1

    return {
        "schema_version": "1.0",
        "ticket": "OMN-15423",
        "completion_status": "blocked_pending_live_catalog_and_activity_evidence",
        "database_ref": "application",
        "physical_seed_database": "omnidash_analytics",
        "ownership_authority": "distributed node contract.yaml -> db_io.db_tables",
        "inventory_projection": "generated; never an ownership allowlist",
        "relation_counts": dict(sorted(counts.items())),
        "retained_live_census": {
            "observed_at": "2026-07-29T00:38:00Z/2026-07-29T00:46:00Z",
            "evidence_source": RETAINED_LIVE_CENSUS_SOURCE,
            "physical_database": "omnidash_analytics",
            "observed_base_tables": 86,
            "observed_views_and_materialized_views": 9,
            "source_created_tables": len(created_tables),
            "source_declared_tables": len(declarations),
            "minimum_unreconciled_live_base_tables": max(0, 86 - len(created_tables)),
            "parity_status": "blocked",
            "reason": (
                "the retained census contains counts but not a complete object-name "
                "export; repository migrations therefore cannot prove name-for-name "
                "live parity without a fresh authorized catalog read"
            ),
        },
        "relations": relation_rows,
        "blocked_relations": blocked_relations,
        "runtime_evidence": {
            "dsn_key_provenance": {
                "OMNIDASH_ANALYTICS_DB_URL": [
                    "src/omnimarket/adapters/asyncpg_adapter.py",
                    "src/omnimarket/config/settings.py",
                    "src/omnimarket/projection/runner.py",
                ]
            },
            "full_day_datname_usename_activity": {
                "status": "blocked",
                "reason": "live database access was outside this build lane's authorization",
                "credentials_captured": False,
            },
            "live_catalog_parity": {
                "status": "blocked",
                "reason": (
                    f"repository sources contain {len(created_tables)} CREATE TABLE "
                    "objects while the retained live census reports 86 base tables; "
                    "exact overlap and the at-least-"
                    f"{max(0, 86 - len(created_tables))}-table gap require an "
                    "authorized catalog read"
                ),
            },
        },
    }


def _render(payload: dict[str, Any]) -> str:
    return json.dumps(payload, indent=2, sort_keys=True) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--check", action="store_true", help="fail if the checked-in evidence is stale"
    )
    mode.add_argument(
        "--write", action="store_true", help="replace the generated evidence file"
    )
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args(argv)
    rendered = _render(build_inventory())
    if args.check:
        if not args.output.is_file():
            print(f"missing generated inventory: {args.output}", file=sys.stderr)
            return 1
        if args.output.read_text(encoding="utf-8") != rendered:
            print(
                f"stale generated inventory: run {Path(__file__).relative_to(REPO_ROOT)}",
                file=sys.stderr,
            )
            return 1
        return 0
    if args.write:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
        print(args.output)
        return 0
    sys.stdout.write(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
