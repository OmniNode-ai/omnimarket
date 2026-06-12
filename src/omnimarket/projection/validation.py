"""Validation for projection API materialization.

Called once at startup after ``build_projection_topic_map()``. Marks entries
as DEGRADED when their declared tables are absent from the database — never
silently excludes them.

The static materialization ratchet is intentionally separate from discovery:
``projection_api.expose: true`` makes a topic readable by the API, but it does
not create the backing table or view. Exposed topics must also have a declared
materialization authority and cold DDL proof before they are accepted by the
ratchet.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, cast

import asyncpg
import yaml

from omnimarket.projection.discovery import (
    _load_projection_api_section,
    _parse_projection_api_sections,
    discover_contracts,
)
from omnimarket.projection.models import ProjectionStatus, ProjectionTableConfig

logger = logging.getLogger(__name__)


class ProtocolDiscoveredContract(Protocol):
    """Projection materialization validation only needs name and path."""

    name: str
    contract_path: Path


class ProtocolAutoWiringManifest(Protocol):
    """Minimal manifest shape consumed by projection validation."""

    @property
    def contracts(self) -> Sequence[ProtocolDiscoveredContract]:
        """Discovered contracts exposed by the manifest."""
        ...


@dataclass(frozen=True)
class ProjectionMaterializationIssue:
    """A missing materialization proof for one exposed projection topic."""

    source_contract: str
    contract_path: Path
    topic: str
    schema_name: str
    table_or_view: str
    code: str
    detail: str

    def format(self) -> str:
        return (
            f"contract={self.source_contract} "
            f"path={self.contract_path} "
            f"topic={self.topic} "
            f"table/view={self.schema_name}.{self.table_or_view} "
            f"{self.detail}"
        )


class ProjectionMaterializationValidationError(ValueError):
    """Raised when an exposed projection lacks materialization proof."""

    def __init__(self, issues: Sequence[ProjectionMaterializationIssue]) -> None:
        self.issues = tuple(issues)
        super().__init__(self._message())

    def _message(self) -> str:
        lines = ["Projection materialization validation failed:"]
        lines.extend(f"- {issue.format()}" for issue in self.issues)
        return "\n".join(lines)


@dataclass(frozen=True)
class _MaterializationAuthority:
    source: str
    detail: str


def validate_projection_materialization_contracts(
    manifest: ProtocolAutoWiringManifest | None = None,
    *,
    contract_names: Iterable[str] | None = None,
) -> tuple[ProjectionMaterializationIssue, ...]:
    """Validate materialization authority for exposed projection topics.

    Each ``projection_api.expose: true`` topic in scope must have both:
    - a declared DDL/materializer authority, such as ``db_io.db_tables``,
      ``metadata.yaml`` ownership, or a node-local migration that creates the
      exposed table/view; and
    - cold DDL proof: a node-local SQL migration that creates the declared
      table or view.

    Args:
        manifest: Pre-built manifest used by tests. ``None`` discovers
            installed omnimarket node contracts.
        contract_names: Optional source-contract scope. Use this while known
            instance-fix tickets are still landing; omit it for full ratchet
            enforcement.

    Returns:
        Tuple of validation issues. Empty means every scoped exposure passed.
    """
    resolved_manifest = (
        cast(ProtocolAutoWiringManifest, discover_contracts())
        if manifest is None
        else manifest
    )
    scoped_names = set(contract_names) if contract_names is not None else None

    issues: list[ProjectionMaterializationIssue] = []

    for contract in resolved_manifest.contracts:
        contract_path = contract.contract_path
        contract_data = _load_yaml_mapping(contract_path)
        source_contract = _source_contract_name(contract, contract_data)
        if scoped_names is not None and source_contract not in scoped_names:
            continue

        section = _load_projection_api_section(contract_path)
        if section is None or section.get("expose") is not True:
            continue

        node_dir = contract_path.parent
        metadata = _load_yaml_mapping(node_dir / "metadata.yaml")
        migration_files = tuple(sorted((node_dir / "migrations").glob("*.sql")))

        for cfg in _parse_projection_api_sections(
            section, source_contract, contract_path
        ):
            authorities = _materialization_authorities(
                cfg=cfg,
                contract_data=contract_data,
                metadata=metadata,
                node_dir=node_dir,
                migration_files=migration_files,
            )
            has_cold_proof = _migration_creates_relation(migration_files, cfg.table)

            if not authorities:
                issues.append(
                    ProjectionMaterializationIssue(
                        source_contract=source_contract,
                        contract_path=contract_path,
                        topic=cfg.topic,
                        schema_name=cfg.schema_name,
                        table_or_view=cfg.table,
                        code="missing_materialization_authority",
                        detail=(
                            "missing materialization authority: declare a DDL "
                            "owner/materializer in metadata ownership, "
                            "db_io.db_tables, or node-local migration DDL"
                        ),
                    )
                )
                continue

            if not has_cold_proof:
                authority_details = ", ".join(
                    authority.detail for authority in authorities
                )
                issues.append(
                    ProjectionMaterializationIssue(
                        source_contract=source_contract,
                        contract_path=contract_path,
                        topic=cfg.topic,
                        schema_name=cfg.schema_name,
                        table_or_view=cfg.table,
                        code="missing_cold_table_proof",
                        detail=(
                            "missing cold DDL proof: declared authority "
                            f"({authority_details}) but no node-local "
                            "migration creates this table/view"
                        ),
                    )
                )

    return tuple(issues)


def assert_projection_materialization_contracts_ready(
    manifest: ProtocolAutoWiringManifest | None = None,
    *,
    contract_names: Iterable[str] | None = None,
) -> None:
    """Raise if any scoped exposed projection lacks materialization proof."""
    issues = validate_projection_materialization_contracts(
        manifest,
        contract_names=contract_names,
    )
    if issues:
        raise ProjectionMaterializationValidationError(issues)


async def validate_topic_map_tables(
    pool: asyncpg.Pool,
    topic_map: dict[str, ProjectionTableConfig],
) -> dict[str, ProjectionTableConfig]:
    """Verify each declared table exists in the database.

    Returns an updated map with DEGRADED status for missing tables.
    Every contract with ``expose: true`` appears in the returned map —
    nothing is excluded silently.

    Args:
        pool: Active asyncpg connection pool.
        topic_map: Map built by :func:`build_projection_topic_map`.

    Returns:
        New dict with the same keys; entries for missing tables have
        ``status=DEGRADED`` and ``degraded_reason`` set.
    """
    result: dict[str, ProjectionTableConfig] = {}

    for topic, cfg in topic_map.items():
        exists = await _table_exists(pool, cfg.schema_name, cfg.table)
        if not exists:
            reason = f"table '{cfg.schema_name}.{cfg.table}' not found at startup"
            logger.warning(
                "Projection API: topic %r DEGRADED — %s",
                topic,
                reason,
            )
            result[topic] = cfg.model_copy(
                update={
                    "status": ProjectionStatus.DEGRADED,
                    "degraded_reason": reason,
                }
            )
        else:
            result[topic] = cfg

    return result


async def _table_exists(
    pool: asyncpg.Pool,
    schema_name: str,
    table_name: str,
) -> bool:
    """Return True if ``schema_name.table_name`` exists in the database."""
    try:
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT 1
                FROM information_schema.tables
                WHERE table_schema = $1
                  AND table_name   = $2
                """,
                schema_name,
                table_name,
            )
        return row is not None
    except Exception as exc:
        logger.warning(
            "Table existence check failed for %s.%s: %s",
            schema_name,
            table_name,
            exc,
        )
        return False


def _source_contract_name(
    contract: ProtocolDiscoveredContract,
    contract_data: dict[str, object],
) -> str:
    raw_name = contract_data.get("name")
    if isinstance(raw_name, str) and raw_name:
        return raw_name
    return contract.name


def _load_yaml_mapping(path: Path) -> dict[str, object]:
    try:
        data = yaml.safe_load(path.read_text())
    except FileNotFoundError:
        return {}
    except Exception as exc:
        logger.warning("Failed to read YAML at %s: %s", path, exc)
        return {}
    return data if isinstance(data, dict) else {}


def _materialization_authorities(
    *,
    cfg: ProjectionTableConfig,
    contract_data: dict[str, object],
    metadata: dict[str, object],
    node_dir: Path,
    migration_files: Sequence[Path],
) -> tuple[_MaterializationAuthority, ...]:
    authorities: list[_MaterializationAuthority] = []
    authorities.extend(_metadata_ownership_authorities(cfg, metadata, node_dir))
    authorities.extend(_db_io_authorities(cfg, contract_data))

    ddl_file = _migration_file_creating_relation(migration_files, cfg.table)
    if ddl_file is not None:
        authorities.append(
            _MaterializationAuthority(
                source="node_migration",
                detail=str(ddl_file.relative_to(node_dir)),
            )
        )

    return tuple(authorities)


def _metadata_ownership_authorities(
    cfg: ProjectionTableConfig,
    metadata: dict[str, object],
    node_dir: Path,
) -> tuple[_MaterializationAuthority, ...]:
    raw_ownership = metadata.get("ownership")
    if not isinstance(raw_ownership, dict):
        return ()

    raw_entry = raw_ownership.get(cfg.table)
    if not isinstance(raw_entry, dict):
        return ()

    ddl_owner = raw_entry.get("ddl_owner")
    migration = raw_entry.get("create_migration")
    if not isinstance(ddl_owner, str) or not ddl_owner:
        return ()
    if not isinstance(migration, str) or not migration:
        return ()

    migration_path = node_dir / migration
    if not migration_path.is_file():
        return ()

    return (
        _MaterializationAuthority(
            source="metadata_ownership",
            detail=f"ownership.{cfg.table}.ddl_owner={ddl_owner}",
        ),
    )


def _db_io_authorities(
    cfg: ProjectionTableConfig,
    contract_data: dict[str, object],
) -> tuple[_MaterializationAuthority, ...]:
    raw_db_io = contract_data.get("db_io")
    if not isinstance(raw_db_io, dict):
        return ()

    raw_tables = raw_db_io.get("db_tables")
    if not isinstance(raw_tables, list):
        return ()

    authorities: list[_MaterializationAuthority] = []
    for raw_table in raw_tables:
        if not isinstance(raw_table, dict):
            continue
        if raw_table.get("name") != cfg.table:
            continue
        migration = raw_table.get("migration")
        access = raw_table.get("access")
        if not isinstance(migration, str) or not migration:
            continue
        if access not in {"write", "read_write", "owner"}:
            continue
        authorities.append(
            _MaterializationAuthority(
                source="db_io",
                detail=f"db_io.db_tables[{cfg.table}].migration={migration}",
            )
        )

    return tuple(authorities)


def _migration_creates_relation(
    migration_files: Sequence[Path],
    relation_name: str,
) -> bool:
    return _migration_file_creating_relation(migration_files, relation_name) is not None


def _migration_file_creating_relation(
    migration_files: Sequence[Path],
    relation_name: str,
) -> Path | None:
    pattern = _create_relation_pattern(relation_name)
    for migration_file in migration_files:
        try:
            sql = migration_file.read_text()
        except Exception as exc:
            logger.warning("Failed to read migration %s: %s", migration_file, exc)
            continue
        if pattern.search(sql):
            return migration_file
    return None


def _create_relation_pattern(relation_name: str) -> re.Pattern[str]:
    quoted = re.escape(relation_name)
    quoted_identifier = re.escape(f'"{relation_name}"')
    name_pattern = f"(?:public\\.)?(?:{quoted}|{quoted_identifier})"
    return re.compile(
        rf"\bCREATE\s+(?:OR\s+REPLACE\s+)?"
        rf"(?:(?:MATERIALIZED\s+)?VIEW|TABLE)"
        rf"(?:\s+IF\s+NOT\s+EXISTS)?\s+{name_pattern}\b",
        re.IGNORECASE | re.MULTILINE,
    )
