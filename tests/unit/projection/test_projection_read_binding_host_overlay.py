"""Tests for per-host projection read binding overlay (OMN-23994).

This module verifies the behavior of ``projection_read_binding_from_host_overlay``
and ``projection_read_binding_from_overlay_env`` when resolving bindings from
node overlays.

Test groups:
- Golden chain: valid host overlay row resolves to a binding and reads rows.
- Host matching: case-insensitive and short-name matching.
- Precedence: read and runtime overlay variables win over host overlay.
- Error chains: missing host, missing file, unset env var, invalid overlay.
- Model validation: duplicate hosts, invalid prefixes, extra keys.
- Order seam: broken read variable prevents host overlay consultation.

All test names contain "host_overlay" so ``-k host_overlay`` selects the chain tests.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

import omnimarket.projection.runner as runner
from omnimarket.models.node_overlays.model_projection_read_binding_overlay import (
    ModelProjectionReadBindingOverlay,
)
from omnimarket.nodes.node_projection_read_effect.handlers.handler_projection_read import (
    HandlerProjectionRead,
)
from omnimarket.nodes.node_projection_read_effect.models import (
    ModelProjectionReadRequest,
)
from omnimarket.nodes.node_projection_read_effect.ports.read_source_resolution import (
    resolve_projection_read_source,
)
from omnimarket.projection.discovery import parse_order_by_clauses
from omnimarket.projection.models import ProjectionTableConfig
from omnimarket.projection.table_reader import (
    ProjectionReadError,
)

# Literal names, not imports: the read variable's constant does not exist
# before this change, and these tests must fail on dev rather than not collect.
_RUNTIME_ENV = "OMNIMARKET_PROJECTION_RUNTIME_BINDING_OVERLAY"
_READ_ENV = "OMNIMARKET_PROJECTION_READ_BINDING_OVERLAY"
_ONEX_SKILL_OVERLAY_ROOTS = "ONEX_SKILL_OVERLAY_ROOTS"
_ONXIDASH_ANALYTICS_DB_URL = "OMNIDASH_ANALYTICS_DB_URL"

_DECISIONS = "onex.snapshot.projection.delegation.decisions.v1"
_TENANT = "820272f9-4aaf-5add-a2df-0af942852ab2"
_COLUMNS = ("correlation_id", "tenant_id", "written_at", "cost_usd")


def _cfg() -> ProjectionTableConfig:
    return ProjectionTableConfig(
        topic=_DECISIONS,
        table="delegation_events",
        schema_name="public",
        relation_schema="public",
        columns=_COLUMNS,
        order_by="written_at DESC",
        order_by_spec=parse_order_by_clauses("written_at DESC", _COLUMNS),
        freshness_column="written_at",
        cursor_column="written_at",
        limit=500,
        bus_backed=True,
        key_columns=("correlation_id",),
        tenant_column="tenant_id",
    )


def _store(path: Path, rows: int) -> Path:
    """A SQLite store holding ``rows`` decisions for the tenant."""
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    try:
        conn.execute(
            "CREATE TABLE delegation_events (correlation_id TEXT NOT NULL UNIQUE, "
            "tenant_id TEXT, written_at TEXT, cost_usd REAL)"
        )
        for i in range(rows):
            conn.execute(
                "INSERT INTO delegation_events VALUES (?, ?, ?, ?)",
                (
                    f"20159400-0000-4000-8000-{i:012d}",
                    _TENANT,
                    "2024-01-01T00:00:00Z",
                    1.0,
                ),
            )
        conn.commit()
    finally:
        conn.close()
    return path


def _write_host_overlay(
    tmp_path: Path,
    root_name: str,
    host: str,
    kafka_bootstrap_servers: str,
    kafka_consumer_group: str,
    database_url_secret_ref: str,
) -> Path:
    """Write a node overlay file with a single binding row for the given host."""
    root = tmp_path / root_name
    overlay_dir = root / "node_projection_read_effect"
    overlay_dir.mkdir(parents=True, exist_ok=True)
    overlay_file = overlay_dir / "overlay.yaml"
    data = {
        "schema_version": "projection_read_binding.v1",
        "bindings": [
            {
                "host": host,
                "kafka_bootstrap_servers": kafka_bootstrap_servers,
                "kafka_consumer_group": kafka_consumer_group,
                "database_url_secret_ref": database_url_secret_ref,
            }
        ],
    }
    overlay_file.write_text(yaml.safe_dump(data))
    return overlay_file


def _write_overlay_file(
    path: Path,
    kafka_bootstrap_servers: str,
    kafka_consumer_group: str,
    database_url: str,
) -> Path:
    """Write a runtime/read overlay file (not a host overlay)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "kafka_bootstrap_servers": kafka_bootstrap_servers,
        "kafka_consumer_group": kafka_consumer_group,
        "database_url": database_url,
    }
    path.write_text(yaml.safe_dump(data))
    return path


@pytest.fixture(autouse=True)
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Delete overlay-related env vars and refuse psycopg2 connections."""
    monkeypatch.delenv(_READ_ENV, raising=False)
    monkeypatch.delenv(_RUNTIME_ENV, raising=False)
    monkeypatch.delenv(_ONEX_SKILL_OVERLAY_ROOTS, raising=False)
    monkeypatch.delenv(_ONXIDASH_ANALYTICS_DB_URL, raising=False)

    import psycopg2

    def _refuse(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError(f"unexpected Postgres connection: {args!r}")

    monkeypatch.setattr(psycopg2, "connect", _refuse)


def _set_host(
    monkeypatch: pytest.MonkeyPatch,
    host: str,
) -> None:
    """Monkeypatch runner.local_host_short_name to return the given host."""
    monkeypatch.setattr(runner, "local_host_short_name", lambda: host)


def _set_overlay_roots(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    root_name: str,
) -> None:
    """Set ONEX_SKILL_OVERLAY_ROOTS to point at the given root."""
    monkeypatch.setenv(_ONEX_SKILL_OVERLAY_ROOTS, str(tmp_path / root_name))


def _set_dsn_env(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    store_name: str = "store.db",
) -> str:
    """Set the DSN env var to a SQLite URL and return the URL."""
    dsn = f"sqlite:///{tmp_path / store_name}"
    monkeypatch.setenv("HOST_OVERLAY_TEST_DSN", dsn)
    return dsn


async def _read_rows(**request: Any) -> Any:
    handler = HandlerProjectionRead(topic_map={_DECISIONS: _cfg()})
    try:
        return await handler.handle(ModelProjectionReadRequest(**request))
    finally:
        await handler.close()


class TestHostOverlayGolden:
    """Golden chain: host overlay row resolves and reads rows."""

    async def test_host_overlay_golden_binding_and_read(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A row for the local host with ONEX_SKILL_OVERLAY_ROOTS set returns a binding
        whose source ends with '#<host>' and resolve_database_url() equals the sqlite dsn."""
        host = "testhost"
        _set_host(monkeypatch, host)
        _set_overlay_roots(monkeypatch, tmp_path, "root1")
        _write_host_overlay(
            tmp_path,
            "root1",
            host,
            "kafka:9092",
            "consumer-group",
            "env:HOST_OVERLAY_TEST_DSN",
        )
        dsn = _set_dsn_env(monkeypatch, tmp_path)
        _store(tmp_path / "store.db", 3)

        binding = runner.projection_read_binding_from_overlay_env()
        assert binding is not None
        assert binding.source.endswith(f"#{host}")
        assert binding.resolve_database_url() == dsn

    async def test_host_overlay_golden_read_chain(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The read node answers ok True with row_count 3."""
        host = "testhost"
        _set_host(monkeypatch, host)
        _set_overlay_roots(monkeypatch, tmp_path, "root1")
        _write_host_overlay(
            tmp_path,
            "root1",
            host,
            "kafka:9092",
            "consumer-group",
            "env:HOST_OVERLAY_TEST_DSN",
        )
        _set_dsn_env(monkeypatch, tmp_path)
        _store(tmp_path / "store.db", 3)

        result = await _read_rows(
            topic=_DECISIONS,
            tenant_id=_TENANT,
            limit=10,
        )
        assert result.ok is True
        assert result.row_count == 3


class TestHostOverlayMatching:
    """Host matching: case-insensitive and short-name matching."""

    async def test_host_overlay_case_insensitive_match(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Host row 'MacHost' matches a local name 'machost'."""
        _set_host(monkeypatch, "machost")
        _set_overlay_roots(monkeypatch, tmp_path, "root1")
        _write_host_overlay(
            tmp_path,
            "root1",
            "MacHost",
            "kafka:9092",
            "consumer-group",
            "env:HOST_OVERLAY_TEST_DSN",
        )
        _set_dsn_env(monkeypatch, tmp_path)
        _store(tmp_path / "store.db", 1)

        binding = runner.projection_read_binding_from_overlay_env()
        assert binding is not None
        assert binding.source.endswith("#MacHost")


class TestHostOverlayPrecedence:
    """Precedence: read and runtime variables win over host overlay."""

    async def test_host_overlay_read_variable_wins(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The read variable wins over the host overlay row."""
        host = "testhost"
        _set_host(monkeypatch, host)
        _set_overlay_roots(monkeypatch, tmp_path, "root1")
        _write_host_overlay(
            tmp_path,
            "root1",
            host,
            "kafka:9092",
            "consumer-group",
            "env:HOST_OVERLAY_TEST_DSN",
        )
        _set_dsn_env(monkeypatch, tmp_path)
        _store(tmp_path / "store.db", 1)

        read_overlay = _write_overlay_file(
            tmp_path / "read_overlay.yaml",
            "kafka-read:9092",
            "read-group",
            "env:HOST_OVERLAY_TEST_DSN",
        )
        monkeypatch.setenv(_READ_ENV, str(read_overlay))

        binding = runner.projection_read_binding_from_overlay_env()
        assert binding is not None
        assert binding.kafka_bootstrap_servers == "kafka-read:9092"
        assert binding.kafka_consumer_group == "read-group"

    async def test_host_overlay_runtime_variable_wins(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The runtime variable wins over the host overlay row."""
        host = "testhost"
        _set_host(monkeypatch, host)
        _set_overlay_roots(monkeypatch, tmp_path, "root1")
        _write_host_overlay(
            tmp_path,
            "root1",
            host,
            "kafka:9092",
            "consumer-group",
            "env:HOST_OVERLAY_TEST_DSN",
        )
        _set_dsn_env(monkeypatch, tmp_path)
        _store(tmp_path / "store.db", 1)

        runtime_overlay = _write_overlay_file(
            tmp_path / "runtime_overlay.yaml",
            "kafka-runtime:9092",
            "runtime-group",
            "env:HOST_OVERLAY_TEST_DSN",
        )
        monkeypatch.setenv(_RUNTIME_ENV, str(runtime_overlay))

        binding = runner.projection_read_binding_from_overlay_env()
        assert binding is not None
        assert binding.kafka_bootstrap_servers == "kafka-runtime:9092"
        assert binding.kafka_consumer_group == "runtime-group"


class TestHostOverlayUnknownHost:
    """Error chains: unknown host, missing file, unset env var."""

    async def test_host_overlay_unknown_host_no_row(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """No row for this host -> binding None; resolve_projection_read_source raises
        ProjectionReadError code projection_binding_unconfigured; the node answers
        ok False, error projection_binding_unconfigured, rows empty and the detail
        mentions node_projection_read_effect; never ok True with zero rows."""
        host = "unknownhost"
        _set_host(monkeypatch, host)
        _set_overlay_roots(monkeypatch, tmp_path, "root1")
        _write_host_overlay(
            tmp_path,
            "root1",
            "otherhost",
            "kafka:9092",
            "consumer-group",
            "env:HOST_OVERLAY_TEST_DSN",
        )
        _set_dsn_env(monkeypatch, tmp_path)
        _store(tmp_path / "store.db", 3)

        binding = runner.projection_read_binding_from_overlay_env()
        assert binding is None

        with pytest.raises(ProjectionReadError) as exc_info:
            resolve_projection_read_source()
        assert exc_info.value.code == "projection_binding_unconfigured"

        result = await _read_rows(
            topic=_DECISIONS,
            tenant_id=_TENANT,
            limit=10,
        )
        assert result.ok is False
        assert result.error == "projection_binding_unconfigured"
        assert result.rows == []
        assert "node_projection_read_effect" in result.detail

    async def test_host_overlay_unknown_host_missing_file(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The overlay file is absent (root set, no file) -> same refusal."""
        host = "testhost"
        _set_host(monkeypatch, host)
        _set_overlay_roots(monkeypatch, tmp_path, "nonexistent_root")
        _set_dsn_env(monkeypatch, tmp_path)
        _store(tmp_path / "store.db", 3)

        binding = runner.projection_read_binding_from_overlay_env()
        assert binding is None

        with pytest.raises(ProjectionReadError) as exc_info:
            resolve_projection_read_source()
        assert exc_info.value.code == "projection_binding_unconfigured"

        result = await _read_rows(
            topic=_DECISIONS,
            tenant_id=_TENANT,
            limit=10,
        )
        assert result.ok is False
        assert result.error == "projection_binding_unconfigured"
        assert result.rows == []
        assert "node_projection_read_effect" in result.detail

    async def test_host_overlay_unknown_host_unset_env(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The row's env variable is unset -> ProjectionReadError
        projection_binding_unconfigured and the node answers ok False."""
        host = "testhost"
        _set_host(monkeypatch, host)
        _set_overlay_roots(monkeypatch, tmp_path, "root1")
        _write_host_overlay(
            tmp_path,
            "root1",
            host,
            "kafka:9092",
            "consumer-group",
            "env:UNSET_DSN_VAR",
        )
        _store(tmp_path / "store.db", 3)

        binding = runner.projection_read_binding_from_overlay_env()
        assert binding is not None

        with pytest.raises(ProjectionReadError) as exc_info:
            resolve_projection_read_source()
        assert exc_info.value.code == "projection_binding_unconfigured"

        result = await _read_rows(
            topic=_DECISIONS,
            tenant_id=_TENANT,
            limit=10,
        )
        assert result.ok is False
        assert result.error == "projection_binding_unconfigured"
        assert result.rows == []


class TestHostOverlayInvalidOverlay:
    """Error chains: invalid overlay files."""

    async def test_host_overlay_invalid_inline_postgres_url(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """An inline 'postgresql://u:pw@h/db' as database_url_secret_ref ->
        ProjectionReadBindingOverlayError naming ONEX_SKILL_OVERLAY_ROOTS and the file;
        the node answers error projection_binding_invalid and the detail never contains
        'pw' or the url; never falls back to a lower source."""
        host = "testhost"
        _set_host(monkeypatch, host)
        _set_overlay_roots(monkeypatch, tmp_path, "root1")
        _write_host_overlay(
            tmp_path,
            "root1",
            host,
            "kafka:9092",
            "consumer-group",
            "postgresql://u:pw@h/db",
        )
        _set_dsn_env(monkeypatch, tmp_path)
        _store(tmp_path / "store.db", 3)

        with pytest.raises(runner.ProjectionReadBindingOverlayError) as exc_info:
            runner.projection_read_binding_from_overlay_env()
        assert "ONEX_SKILL_OVERLAY_ROOTS" in str(exc_info.value)
        assert "overlay.yaml" in str(exc_info.value)

        result = await _read_rows(
            topic=_DECISIONS,
            tenant_id=_TENANT,
            limit=10,
        )
        assert result.ok is False
        assert result.error == "projection_binding_invalid"
        assert "pw" not in result.detail
        assert "postgresql://u:pw@h/db" not in result.detail

    async def test_host_overlay_invalid_yaml_not_mapping(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A YAML that is not a mapping -> ProjectionReadBindingOverlayError."""
        host = "testhost"
        _set_host(monkeypatch, host)
        _set_overlay_roots(monkeypatch, tmp_path, "root1")
        root = tmp_path / "root1"
        overlay_dir = root / "node_projection_read_effect"
        overlay_dir.mkdir(parents=True, exist_ok=True)
        overlay_file = overlay_dir / "overlay.yaml"
        overlay_file.write_text("- just a list\n- not a mapping\n")
        _set_dsn_env(monkeypatch, tmp_path)
        _store(tmp_path / "store.db", 3)

        with pytest.raises(runner.ProjectionReadBindingOverlayError) as exc_info:
            runner.projection_read_binding_from_overlay_env()
        assert "ONEX_SKILL_OVERLAY_ROOTS" in str(exc_info.value)
        assert "overlay.yaml" in str(exc_info.value)

        result = await _read_rows(
            topic=_DECISIONS,
            tenant_id=_TENANT,
            limit=10,
        )
        assert result.ok is False
        assert result.error == "projection_binding_invalid"


class TestHostOverlayModelValidation:
    """Model validation: duplicate hosts, invalid prefixes, extra keys."""

    async def test_host_overlay_duplicate_host_refused(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Two rows naming one host ignoring case -> the model refuses (pydantic
        ValidationError)."""
        host = "testhost"
        _set_host(monkeypatch, host)
        _set_overlay_roots(monkeypatch, tmp_path, "root1")
        root = tmp_path / "root1"
        overlay_dir = root / "node_projection_read_effect"
        overlay_dir.mkdir(parents=True, exist_ok=True)
        overlay_file = overlay_dir / "overlay.yaml"
        data = {
            "schema_version": "projection_read_binding.v1",
            "bindings": [
                {
                    "host": "TestHost",
                    "kafka_bootstrap_servers": "kafka:9092",
                    "kafka_consumer_group": "group1",
                    "database_url_secret_ref": "env:HOST_OVERLAY_TEST_DSN",
                },
                {
                    "host": "testhost",
                    "kafka_bootstrap_servers": "kafka:9092",
                    "kafka_consumer_group": "group2",
                    "database_url_secret_ref": "env:HOST_OVERLAY_TEST_DSN",
                },
            ],
        }
        overlay_file.write_text(yaml.safe_dump(data))
        _set_dsn_env(monkeypatch, tmp_path)
        _store(tmp_path / "store.db", 3)

        with pytest.raises(runner.ProjectionReadBindingOverlayError) as exc_info:
            runner.projection_read_binding_from_overlay_env()
        assert exc_info.value.cause_type == "NodeOverlayError"
        assert exc_info.value.variable == "ONEX_SKILL_OVERLAY_ROOTS"

    async def test_host_overlay_invalid_prefix_refused(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A row prefix other than env:/settings: refused."""
        host = "testhost"
        _set_host(monkeypatch, host)
        _set_overlay_roots(monkeypatch, tmp_path, "root1")
        _write_host_overlay(
            tmp_path,
            "root1",
            host,
            "kafka:9092",
            "consumer-group",
            "invalid:prefix",
        )
        _set_dsn_env(monkeypatch, tmp_path)
        _store(tmp_path / "store.db", 3)

        with pytest.raises(runner.ProjectionReadBindingOverlayError) as exc_info:
            runner.projection_read_binding_from_overlay_env()
        assert exc_info.value.cause_type == "NodeOverlayError"
        assert exc_info.value.variable == "ONEX_SKILL_OVERLAY_ROOTS"

    async def test_host_overlay_extra_key_refused(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """An extra key refused."""
        host = "testhost"
        _set_host(monkeypatch, host)
        _set_overlay_roots(monkeypatch, tmp_path, "root1")
        root = tmp_path / "root1"
        overlay_dir = root / "node_projection_read_effect"
        overlay_dir.mkdir(parents=True, exist_ok=True)
        overlay_file = overlay_dir / "overlay.yaml"
        data = {
            "schema_version": "projection_read_binding.v1",
            "bindings": [
                {
                    "host": host,
                    "kafka_bootstrap_servers": "kafka:9092",
                    "kafka_consumer_group": "consumer-group",
                    "database_url_secret_ref": "env:HOST_OVERLAY_TEST_DSN",
                    "extra_key": "should be refused",
                }
            ],
        }
        overlay_file.write_text(yaml.safe_dump(data))
        _set_dsn_env(monkeypatch, tmp_path)
        _store(tmp_path / "store.db", 3)

        with pytest.raises(runner.ProjectionReadBindingOverlayError) as exc_info:
            runner.projection_read_binding_from_overlay_env()
        assert exc_info.value.cause_type == "NodeOverlayError"
        assert exc_info.value.variable == "ONEX_SKILL_OVERLAY_ROOTS"


class TestHostOverlayOrderSeam:
    """Order seam: broken read variable prevents host overlay consultation."""

    async def test_host_overlay_broken_read_variable_never_consults_host(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """With the read variable set to a broken file the host overlay is never
        consulted (the error is projection_binding_invalid naming the read variable)."""
        host = "testhost"
        _set_host(monkeypatch, host)
        _set_overlay_roots(monkeypatch, tmp_path, "root1")
        _write_host_overlay(
            tmp_path,
            "root1",
            host,
            "kafka:9092",
            "consumer-group",
            "env:HOST_OVERLAY_TEST_DSN",
        )
        _set_dsn_env(monkeypatch, tmp_path)
        _store(tmp_path / "store.db", 3)

        broken_file = tmp_path / "broken_read_overlay.yaml"
        broken_file.write_text("not valid yaml: [")
        monkeypatch.setenv(_READ_ENV, str(broken_file))

        with pytest.raises(runner.ProjectionReadBindingOverlayError) as exc_info:
            runner.projection_read_binding_from_overlay_env()
        assert "OMNIMARKET_PROJECTION_READ_BINDING_OVERLAY" in str(exc_info.value)
        assert "broken_read_overlay.yaml" in str(exc_info.value)

        result = await _read_rows(
            topic=_DECISIONS,
            tenant_id=_TENANT,
            limit=10,
        )
        assert result.ok is False
        assert result.error == "projection_binding_invalid"


_ROW = {
    "host": "H",
    "kafka_bootstrap_servers": "k:1",
    "kafka_consumer_group": "g",
    "database_url_secret_ref": "env:X",
}


@pytest.mark.parametrize(
    "document",
    [
        {
            "schema_version": "projection_read_binding.v1",
            "bindings": [_ROW, {**_ROW, "host": "h"}],
        },
        {
            "schema_version": "projection_read_binding.v1",
            "bindings": [{**_ROW, "database_url_secret_ref": "postgresql://u:p@h/d"}],
        },
        {
            "schema_version": "projection_read_binding.v1",
            "bindings": [{**_ROW, "x": 1}],
        },
        {"schema_version": "projection_read_binding.v1", "bindings": []},
        {"schema_version": "other.v1", "bindings": [_ROW]},
    ],
    ids=["duplicate_host", "inline_url", "extra_key", "no_rows", "wrong_schema"],
)
def test_host_overlay_model_refuses_each_malformed_document(
    document: dict[str, Any],
) -> None:
    with pytest.raises(ValidationError):
        ModelProjectionReadBindingOverlay.model_validate(document)


def test_host_overlay_model_accepts_the_well_formed_document() -> None:
    parsed = ModelProjectionReadBindingOverlay.model_validate(
        {"schema_version": "projection_read_binding.v1", "bindings": [_ROW]}
    )
    assert parsed.bindings[0].database_url_secret_ref == "env:X"


async def test_host_overlay_refusal_keeps_the_prefix_the_queue_status_reads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Seam with the queue status (the queue-status skill of the plugin repository).

    Its liveness line prints ``liveness UNKNOWN: projection_binding_unconfigured: neither
    OMNIMARKET_PROJECTION_READ_BINDING_OVERLAY ...`` from this node's refusal, and its tests
    assert that start of the line. A host with no row must still refuse with exactly that
    start, so an unreadable projection stays UNKNOWN and never reads as a count of zero.
    """
    _set_host(monkeypatch, "no-such-host")
    result = await _read_rows(topic=_DECISIONS, tenant_id=_TENANT)

    assert result.ok is False
    assert result.rows == []
    assert result.error == "projection_binding_unconfigured"
    assert result.detail is not None
    assert result.detail.startswith(
        "neither OMNIMARKET_PROJECTION_READ_BINDING_OVERLAY (the projection read binding) "
        "nor OMNIMARKET_PROJECTION_RUNTIME_BINDING_OVERLAY (the runtime binding a read "
        "falls back to) is set"
    )
