# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-14015: local delegation evidence DB target is a config choice, not a hardcode.

Before OMN-14015 ``LocalDelegationDispatchPort`` baked
``SqliteDatabaseAdapter(default_evidence_db_path())`` into its constructor and
``select_delegation_dispatch_port`` passed no ``evidence_db`` — so CLI delegation
evidence could only ever land in a local SQLite side-target. These tests prove
the target is now resolved from the projection runtime binding overlay (the same
mechanism projection runners use):

* no overlay configured -> the canonical local SQLite target (byte-identical to
  the prior hardcoded default -> golden replays unaffected);
* an overlay with a Postgres ``database_url`` -> the sync Postgres substrate
  adapter targeting that DSN (no code branch, purely by config);
* an overlay with a ``sqlite:`` ``database_url`` -> a SQLite adapter at that path;
* the composition root (``select_delegation_dispatch_port``) injects the resolved
  adapter through the port's ``evidence_db`` seam.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from omnimarket.nodes.node_delegate_skill_orchestrator.ports.evidence_db_resolution import (
    resolve_local_delegation_evidence_db,
)
from omnimarket.nodes.node_delegate_skill_orchestrator.ports.port_selection import (
    select_delegation_dispatch_port,
)
from omnimarket.projection.postgres_sync_database import PostgresSyncProjectionAdapter
from omnimarket.projection.runner import PROJECTION_RUNTIME_BINDING_OVERLAY_ENV
from omnimarket.projection.sqlite_database import (
    SqliteDatabaseAdapter,
    default_evidence_db_path,
)

_POSTGRES_DSN = "postgresql://role_omnidash:secret@dev-postgres:5432/omnidash_analytics"


def _write_overlay(tmp_path: Path, database_url: str) -> Path:
    overlay = tmp_path / "projection_runtime_binding.yaml"
    overlay.write_text(
        "kafka_bootstrap_servers: dev-redpanda:9092\n"
        "kafka_consumer_group: omnimarket-projections-v1\n"
        f"database_url: {database_url}\n",
        encoding="utf-8",
    )
    return overlay


def test_no_overlay_resolves_local_sqlite_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No overlay env -> the canonical local SQLite target (prior default)."""
    monkeypatch.delenv(PROJECTION_RUNTIME_BINDING_OVERLAY_ENV, raising=False)

    adapter = resolve_local_delegation_evidence_db()

    assert isinstance(adapter, SqliteDatabaseAdapter)
    # The resolved path must equal the canonical default, byte-for-byte, so the
    # bus-less CLI + golden replays are unchanged from the pre-OMN-14015 hardcode.
    assert adapter._db_path == default_evidence_db_path()


def test_overlay_postgres_dsn_resolves_postgres_adapter(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Overlay with a Postgres database_url -> the sync Postgres substrate adapter."""
    overlay = _write_overlay(tmp_path, _POSTGRES_DSN)
    monkeypatch.setenv(PROJECTION_RUNTIME_BINDING_OVERLAY_ENV, str(overlay))

    adapter = resolve_local_delegation_evidence_db()

    assert isinstance(adapter, PostgresSyncProjectionAdapter)
    assert adapter._dsn == _POSTGRES_DSN


def test_overlay_sqlite_dsn_resolves_sqlite_adapter_at_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Overlay with a sqlite: database_url -> a SQLite adapter at that explicit path."""
    target = tmp_path / "evidence.sqlite"
    # target is absolute, so the four-slash ``sqlite:////abs`` form round-trips.
    overlay = _write_overlay(tmp_path, f"sqlite:///{target}")
    monkeypatch.setenv(PROJECTION_RUNTIME_BINDING_OVERLAY_ENV, str(overlay))

    adapter = resolve_local_delegation_evidence_db()

    assert isinstance(adapter, SqliteDatabaseAdapter)
    assert adapter._db_path == target


def test_overlay_unsupported_scheme_raises(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A database_url with an unsupported scheme fails loud, not silently SQLite."""
    overlay = _write_overlay(tmp_path, "mysql://user:pass@host:3306/db")
    monkeypatch.setenv(PROJECTION_RUNTIME_BINDING_OVERLAY_ENV, str(overlay))

    with pytest.raises(ValueError, match="unsupported delegation evidence"):
        resolve_local_delegation_evidence_db()


def test_composition_root_injects_resolved_adapter_no_overlay(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """select_delegation_dispatch_port fills the port's evidence_db seam from config."""
    monkeypatch.delenv(PROJECTION_RUNTIME_BINDING_OVERLAY_ENV, raising=False)

    port = select_delegation_dispatch_port(event_bus=None)

    # The local port received a config-resolved adapter (the SQLite default here),
    # not a baked-in constructor default reached by omission.
    assert isinstance(port._evidence_db, SqliteDatabaseAdapter)  # type: ignore[attr-defined]
    assert port._evidence_db._db_path == default_evidence_db_path()  # type: ignore[attr-defined]


def test_composition_root_injects_postgres_adapter_from_overlay(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """With a Postgres overlay, the CLI port targets the substrate purely by config."""
    overlay = _write_overlay(tmp_path, _POSTGRES_DSN)
    monkeypatch.setenv(PROJECTION_RUNTIME_BINDING_OVERLAY_ENV, str(overlay))

    port = select_delegation_dispatch_port(event_bus=None)

    assert isinstance(port._evidence_db, PostgresSyncProjectionAdapter)  # type: ignore[attr-defined]
    assert port._evidence_db._dsn == _POSTGRES_DSN  # type: ignore[attr-defined]
