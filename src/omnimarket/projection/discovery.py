"""Dynamic projection topic discovery from runtime contract registry.

Topic map is built once at startup from the pinned contract state at that
moment. Runtime changes to contracts require a server restart to take effect.

Invariants enforced here:
- Only contracts with ``projection_api.expose: true`` are included.
- Topic names, columns, table, and ordering must be explicitly declared.
- Convention-based defaults (directory names, column-name scanning) are
  never applied.
- Tables in non-whitelisted schemas are rejected at startup.
- Private functions from omnibase_infra are never called.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import yaml

from omnimarket.projection.models import ProjectionStatus, ProjectionTableConfig

logger = logging.getLogger(__name__)

# Only tables in these schemas may be queried through the projection API.
ALLOWED_SCHEMAS: frozenset[str] = frozenset({"public", "omnidash_analytics"})

# Default limit when not declared in contract.
_DEFAULT_LIMIT = 100


class ProtocolDiscoveredContract(Protocol):
    """Projection discovery only needs the public contract name and path."""

    name: str
    contract_path: Path


class ProtocolAutoWiringManifest(Protocol):
    """Minimal manifest shape consumed by projection topic discovery."""

    @property
    def contracts(self) -> Sequence[ProtocolDiscoveredContract]:
        """Discovered contracts exposed by the manifest."""
        ...


@dataclass(frozen=True)
class _DiscoveredContract:
    name: str
    contract_path: Path


@dataclass(frozen=True)
class _AutoWiringManifest:
    contracts: tuple[_DiscoveredContract, ...]


def discover_contracts() -> _AutoWiringManifest:
    """Discover omnimarket node contracts from the installed package tree.

    The projection API only needs contract files owned by this package. Reading
    those files directly keeps startup deterministic for CI and deployed wheels
    without importing handler modules or depending on a newer infra runtime
    helper than the package lock can install.
    """
    nodes_dir = Path(__file__).resolve().parents[1] / "nodes"
    contracts: list[_DiscoveredContract] = []
    for contract_path in sorted(nodes_dir.glob("*/contract.yaml")):
        node_name = contract_path.parent.name
        try:
            data = yaml.safe_load(contract_path.read_text())
        except Exception as exc:
            logger.warning("Failed to read contract at %s: %s", contract_path, exc)
            data = None

        if isinstance(data, dict) and isinstance(data.get("name"), str):
            node_name = data["name"]

        contracts.append(
            _DiscoveredContract(name=node_name, contract_path=contract_path)
        )

    return _AutoWiringManifest(contracts=tuple(contracts))


def build_projection_topic_map(
    manifest: ProtocolAutoWiringManifest | None = None,
) -> dict[str, ProjectionTableConfig]:
    """Build topic -> table mapping from discovered contracts.

    Steps:
    1. Call ``discover_contracts()`` if no manifest is provided.
    2. Filter to contracts whose ``projection_api.expose`` is exactly ``True``.
    3. Validate required fields (topic, table, columns).
    4. Validate table schema against ``ALLOWED_SCHEMAS``.
    5. Return the complete topic map (entries may be DEGRADED, never excluded
       silently).

    Never applies convention-based defaults for topic, columns, or ordering.

    Args:
        manifest: Pre-built manifest (used in tests). Pass ``None`` to trigger
            live entry-point discovery.

    Returns:
        Mapping from topic string to :class:`ProjectionTableConfig`. Entries
        with invalid configuration are excluded with a logged error. Entries
        with valid configuration but potentially missing tables are included
        with ``status=OK`` — table existence is checked separately by
        :func:`omnimarket.projection.validation.validate_topic_map_tables`.
    """
    resolved_manifest = discover_contracts() if manifest is None else manifest

    topic_map: dict[str, ProjectionTableConfig] = {}

    for contract in resolved_manifest.contracts:
        contract_path: Path = contract.contract_path
        node_name: str = contract.name

        section = _load_projection_api_section(contract_path)
        if section is None:
            # No projection_api section — not exposed, skip silently.
            continue

        if not section.get("expose", False):
            # Section present but expose != true — skip silently.
            continue

        cfg = _parse_projection_api_section(section, node_name, contract_path)
        if cfg is None:
            # Parse failure already logged inside _parse_projection_api_section.
            continue

        if cfg.topic in topic_map:
            logger.error(
                "Duplicate projection_api topic %r declared by %r and %r — "
                "second declaration ignored",
                cfg.topic,
                topic_map[cfg.topic].source_contract,
                node_name,
            )
            continue

        topic_map[cfg.topic] = cfg
        logger.info(
            "Projection API: registered topic %r -> table %r (contract: %s)",
            cfg.topic,
            cfg.table,
            node_name,
        )

    if not topic_map:
        logger.warning(
            "Projection API: no topics discovered. "
            "Ensure at least one contract has projection_api.expose: true."
        )
    else:
        logger.info(
            "Projection API topic map built at startup "
            "(restart required to refresh): %d topic(s) registered",
            len(topic_map),
        )

    return topic_map


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _load_projection_api_section(contract_path: Path) -> dict[str, object] | None:
    """Read the ``projection_api`` section from a contract.yaml file.

    Uses only file I/O — no private discovery internals.

    Returns:
        The section dict if present, or ``None`` if the file cannot be read
        or the section is absent.
    """
    try:
        data = yaml.safe_load(contract_path.read_text())
    except Exception as exc:
        logger.warning("Failed to read contract at %s: %s", contract_path, exc)
        return None

    if not isinstance(data, dict):
        return None

    section = data.get("projection_api")
    if not isinstance(section, dict):
        return None

    return section


def _parse_projection_api_section(
    section: dict[str, object],
    node_name: str,
    contract_path: Path,
) -> ProjectionTableConfig | None:
    """Parse and validate a ``projection_api`` YAML section.

    Returns:
        A :class:`ProjectionTableConfig` on success, or ``None`` if any
        required field is missing or invalid.
    """
    topic = section.get("topic")
    if not topic or not isinstance(topic, str):
        logger.error(
            "Contract %r (path: %s): projection_api.topic is required "
            "when expose: true — contract excluded",
            node_name,
            contract_path,
        )
        return None

    table = section.get("table")
    if not table or not isinstance(table, str):
        logger.error(
            "Contract %r (path: %s): projection_api.table is required "
            "when expose: true — contract excluded",
            node_name,
            contract_path,
        )
        return None

    raw_columns = section.get("columns")
    if (
        not raw_columns
        or not isinstance(raw_columns, list)
        or len(raw_columns) == 0
        or any(not isinstance(column, str) or not column for column in raw_columns)
    ):
        logger.error(
            "Contract %r (path: %s): projection_api.columns is required "
            "and must be a non-empty list of strings when expose: true — "
            "contract excluded",
            node_name,
            contract_path,
        )
        return None

    columns: tuple[str, ...] = tuple(raw_columns)

    # Schema resolution: honour explicit schema field; fall back to "public".
    raw_schema = section.get("schema", "public")
    if not isinstance(raw_schema, str):
        logger.error(
            "Contract %r (path: %s): projection_api.schema must be a string "
            "when expose: true — contract excluded",
            node_name,
            contract_path,
        )
        return None

    schema_name: str = raw_schema

    if schema_name not in ALLOWED_SCHEMAS:
        logger.error(
            "Contract %r (path: %s): projection_api.schema %r is not in "
            "ALLOWED_SCHEMAS %s — contract excluded",
            node_name,
            contract_path,
            schema_name,
            sorted(ALLOWED_SCHEMAS),
        )
        return None

    # Optional fields — absent means undefined/unknown (not defaulted).
    raw_order_by = section.get("order_by")
    if raw_order_by is not None and not isinstance(raw_order_by, str):
        logger.error(
            "Contract %r (path: %s): projection_api.order_by must be a string "
            "when present — contract excluded",
            node_name,
            contract_path,
        )
        return None
    order_by: str | None = raw_order_by

    raw_freshness = section.get("freshness_column")
    if raw_freshness is not None and not isinstance(raw_freshness, str):
        logger.error(
            "Contract %r (path: %s): projection_api.freshness_column must be "
            "a string when present — contract excluded",
            node_name,
            contract_path,
        )
        return None
    freshness_column: str | None = raw_freshness

    raw_limit = section.get("limit")
    if raw_limit is None:
        limit = _DEFAULT_LIMIT
    elif type(raw_limit) is int and raw_limit > 0:
        limit = raw_limit
    else:
        logger.error(
            "Contract %r (path: %s): projection_api.limit must be a positive "
            "integer when present — contract excluded",
            node_name,
            contract_path,
        )
        return None

    return ProjectionTableConfig(
        topic=topic,
        table=table,
        schema_name=schema_name,
        columns=columns,
        order_by=order_by,
        freshness_column=freshness_column,
        limit=limit,
        source_contract=node_name,
        status=ProjectionStatus.OK,
        degraded_reason="",
    )
