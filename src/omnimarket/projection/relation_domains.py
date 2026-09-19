# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Resolve a projection relation's security domain from its OWNING CONTRACT.

OMN-18774. Whether a projection write carries a ``tenant_id`` is a property of
the relation's declared domain, not of its name. The runtime already treats it
that way: ``handler_wiring`` maps the contract's ``db_io.db_tables[].schema`` to
an operation class, and the INTERNAL and CATALOG classes refuse a ``tenant_id``
key outright. A handler that stamps the key by hand for a relation the contract
declares ``omninode_internal`` is therefore writing a row the kernel will
reject -- which is exactly what happened to the sync generation projection: it
could not write ``generation_events`` on a runtime-kernel pod at all, and the
divergence stayed invisible because the .201 dev lane runs the async twin.

This module gives the handlers the same fact the runtime resolves, read from
the same declaration, so the two cannot drift apart.

Fail-closed, per Operating Rule 8: an undeclared relation RAISES rather than
defaulting to a domain. A silent default here would re-create the defect in the
opposite direction -- a tenant relation quietly written unattributed.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import yaml
from omnibase_core.enums.enum_database_schema_domain import EnumDatabaseSchemaDomain

#: Canonical schema-name -> security-domain mapping.
#:
#: Mirrors ``omnibase_infra.topology.application_database._EXPECTED_SCHEMAS``.
#: It is restated rather than imported because that name is private to the
#: topology module; ``tests/test_omn18774_internal_relation_tenant_posture.py``
#: pins the two against each other, so a schema added there and not here is a
#: red test rather than a relation this module refuses to classify.
SCHEMA_DOMAINS: Mapping[str, EnumDatabaseSchemaDomain] = {
    "action_authorization_claim": EnumDatabaseSchemaDomain.OMNINODE_INTERNAL,
    "public": EnumDatabaseSchemaDomain.TENANT,
    "tenant": EnumDatabaseSchemaDomain.TENANT,
    "omninode_internal": EnumDatabaseSchemaDomain.OMNINODE_INTERNAL,
    "platform_catalog": EnumDatabaseSchemaDomain.PLATFORM_CATALOG,
}

NODES_DIR: Path = Path(__file__).resolve().parent.parent / "nodes"


class UndeclaredRelationError(KeyError):
    """A relation no node contract declares, asked for its domain."""


class ConflictingRelationDomainError(ValueError):
    """One relation declared under two different security domains."""


class RelationDomainMismatchError(ValueError):
    """A writer's hard-coded domain assumption no longer matches the contract.

    Distinct from :class:`ConflictingRelationDomainError`, which is about a
    contradiction BETWEEN contracts. This one is about a contradiction between
    ONE contract and one writer, and the two want different remedies: the
    first is fixed by classifying the relation, the second by changing the
    statement that assumed the old classification.
    """


@dataclass(frozen=True)
class RelationDeclaration:
    """One ``db_io.db_tables`` entry, as its owning node contract writes it."""

    relation: str
    node: str
    schema: str
    domain: EnumDatabaseSchemaDomain


def iter_contract_relation_declarations(
    nodes_dir: Path | None = None,
) -> Iterator[RelationDeclaration]:
    """Yield every classifiable relation declaration in the node tree.

    A declaration whose ``schema`` is absent from :data:`SCHEMA_DOMAINS` is
    skipped rather than guessed -- ``db_io.db_tables`` also carries entries
    naming a physical database (``omnidash_analytics``) or a caller-supplied
    key, which are not security domains.
    """
    for contract in sorted((nodes_dir or NODES_DIR).glob("*/contract.yaml")):
        documents = [
            document
            for document in yaml.safe_load_all(contract.read_text(encoding="utf-8"))
            if isinstance(document, dict)
        ]
        merged: dict[str, object] = {}
        for document in documents:
            merged.update(document)
        db_io = merged.get("db_io")
        if not isinstance(db_io, dict):
            continue
        tables = db_io.get("db_tables")
        if not isinstance(tables, list):
            continue
        for table in tables:
            if not isinstance(table, dict):
                continue
            name = table.get("name")
            schema = table.get("schema")
            if not isinstance(name, str) or not isinstance(schema, str):
                continue
            domain = SCHEMA_DOMAINS.get(schema.strip().strip('"'))
            if domain is None:
                continue
            yield RelationDeclaration(
                relation=name,
                node=contract.parent.name,
                schema=schema,
                domain=domain,
            )


def load_declared_relation_domains(
    nodes_dir: Path | None = None,
) -> dict[str, EnumDatabaseSchemaDomain]:
    """Read every node contract's ``db_io.db_tables`` into relation -> domain.

    A relation declared twice under the SAME domain is fine -- several nodes
    legitimately share a projection surface. A relation declared under two
    DIFFERENT domains raises: resolving it to either one would invent the
    classification this module exists to read.
    """
    resolved: dict[str, EnumDatabaseSchemaDomain] = {}
    owners: dict[str, set[str]] = {}
    for declaration in iter_contract_relation_declarations(nodes_dir):
        owners.setdefault(declaration.relation, set()).add(declaration.node)
        previous = resolved.get(declaration.relation)
        if previous is not None and previous is not declaration.domain:
            raise ConflictingRelationDomainError(
                f"{declaration.relation} is declared both {previous.value} and "
                f"{declaration.domain.value} by "
                f"{sorted(owners[declaration.relation])}; classify it in the "
                "owning contracts before any writer can resolve it"
            )
        resolved[declaration.relation] = declaration.domain
    return resolved


@lru_cache(maxsize=1)
def _cached_domains() -> Mapping[str, EnumDatabaseSchemaDomain]:
    """The declarations, read once per process.

    Caching is deliberate and safe: contracts are packaged artifacts
    (``pyproject.toml`` ``artifacts = ["src/omnimarket/**/*.yaml", ...]``), so
    within one process they cannot change. A handler must not pay a directory
    walk per event, and re-reading would make the stamp decision depend on
    when in the process lifetime the write happened. Tests that write a
    synthetic tree call :func:`load_declared_relation_domains` directly, or
    :func:`reset_declared_relation_domains_cache` to drop this one.
    """
    return load_declared_relation_domains()


def reset_declared_relation_domains_cache() -> None:
    """Drop the process-lifetime cache. For tests only."""
    _cached_domains.cache_clear()


def declared_relation_domain(table: str) -> EnumDatabaseSchemaDomain:
    """The security domain the owning contract declares for ``table``."""
    try:
        return _cached_domains()[table]
    except KeyError as exc:
        raise UndeclaredRelationError(
            f"{table!r} is declared by no node contract's db_io.db_tables; its "
            "security domain cannot be resolved, and guessing one is how an "
            "internal relation acquires a tenant posture no writer can satisfy "
            "(OMN-18774)"
        ) from exc


def tenant_write_stamp(*, table: str) -> dict[str, str]:
    """The ``tenant_id`` key a write to ``table`` must carry, per its contract.

    ``{}`` for an ``OMNINODE_INTERNAL`` or ``PLATFORM_CATALOG`` relation: the
    operator ruled on 2026-09-14 (``docs/tracking/ROLLING_WORK_LEDGER.md:654``)
    that those relations receive no tenant stamping, and the runtime's
    ``InternalProjectionTableOperation`` refuses the key regardless.

    The house-tenant stamp for a ``TENANT`` relation, unchanged --
    :func:`omnimarket.projection.tenant_isolation.house_tenant_write_stamp`
    remains the one canonical implementation of the writer half of the
    2026-08-02 house-tenant ruling, and this function decides only WHETHER it
    applies, never what it resolves to.
    """
    from omnimarket.projection.tenant_isolation import house_tenant_write_stamp

    if declared_relation_domain(table) is EnumDatabaseSchemaDomain.TENANT:
        return house_tenant_write_stamp(table=table)
    return {}


def assert_internal_relation(table: str) -> None:
    """Refuse to proceed unless ``table`` is declared INTERNAL or CATALOG.

    A raw-SQL writer that has removed its ``tenant_id`` column on the strength
    of a relation's classification has hard-coded that classification into its
    statement. This turns a later re-classification from a silent stream of
    unattributed rows in a tenant-scoped table into a refusal naming the
    relation and the domain it now carries.

    An ordinary ``assert`` would not do: it is stripped under ``python -O``,
    which is exactly the interpreter a container is most likely to run.
    """
    domain = declared_relation_domain(table)
    if domain is EnumDatabaseSchemaDomain.TENANT:
        raise RelationDomainMismatchError(
            f"{table!r} is now declared {domain.value}, but its writer issues a "
            "statement that names no tenant_id and binds no app.tenant_id "
            "because the relation was classified internal (OMN-18774). "
            "Re-classifying it requires restoring the tenant stamp and the "
            "GUC binding in the same change, not just the contract."
        )
