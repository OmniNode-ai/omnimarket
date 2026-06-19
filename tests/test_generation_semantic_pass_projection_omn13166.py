# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-13166 — generation_events distinguishes contract_passed from semantic pass.

The 2026-06-16 gate-zero stability SEA cell projected a contract_passed=true row
for a behaviorally wrong handler. The projection must now carry a separate
behavioral verdict so a shape-valid-but-wrong generation is visibly NOT a
task-behavior pass.

Tests (written from the acceptance criteria):
1. Migration 0014 declares semantic_checked + semantic_passed (idempotent).
2. The contract points generation_events at the 0014 migration.
3. The sync live-runtime write path (HandlerProjectionDelegation) populates both
   fields on the upserted row, independent of contract_passed.
4. The async runner write path emits both columns in its INSERT.
"""

from __future__ import annotations

from pathlib import Path

import yaml

_NODE_DIR = (
    Path(__file__).parent.parent / "src/omnimarket/nodes/node_projection_delegation"
)
MIGRATION_PATH = _NODE_DIR / "migrations/0014_generation_semantic_pass.sql"
CONTRACT_PATH = _NODE_DIR / "contract.yaml"
ASYNC_HANDLER_PATH = _NODE_DIR / "handlers/handler_delegation.py"

SEMANTIC_COLUMNS = ("semantic_checked", "semantic_passed")


# ---------------------------------------------------------------------------
# 1. Migration declares both semantic columns
# ---------------------------------------------------------------------------


def test_migration_file_exists() -> None:
    assert MIGRATION_PATH.exists(), f"Migration not found: {MIGRATION_PATH}"


def test_migration_declares_semantic_columns() -> None:
    sql = MIGRATION_PATH.read_text()
    for column in SEMANTIC_COLUMNS:
        assert column in sql, f"Migration must add {column} column"


def test_migration_is_idempotent_and_cites_ticket() -> None:
    sql = MIGRATION_PATH.read_text()
    assert "IF NOT EXISTS" in sql
    assert "OMN-13166" in sql


# ---------------------------------------------------------------------------
# 2. Contract points generation_events at the new migration
# ---------------------------------------------------------------------------


def test_contract_references_generation_migration() -> None:
    # OMN-13350: the contract now points generation_events at the LATEST
    # generation migration (0015_generation_corpus_acceptance.sql). The 0014
    # semantic columns are still applied — node migrations apply in filename
    # order, so a later migration superseding the contract pointer does not skip
    # 0014.
    contract = yaml.safe_load(CONTRACT_PATH.read_text())
    tables = contract["db_io"]["db_tables"]
    gen = next(t for t in tables if t["name"] == "generation_events")
    assert gen["migration"] == "0015_generation_corpus_acceptance.sql"


def test_contract_exposes_semantic_columns() -> None:
    """The projection API exposure must surface both verdict columns."""
    contract = yaml.safe_load(CONTRACT_PATH.read_text())
    exposures = contract["projection_api"]["exposures"]
    gen = next(e for e in exposures if e["table"] == "generation_events")
    for column in SEMANTIC_COLUMNS:
        assert column in gen["columns"], f"exposure must surface {column}"


# ---------------------------------------------------------------------------
# 3. Sync live-runtime write path populates both fields
# ---------------------------------------------------------------------------


def _project_generation(payload_extra: dict[str, object]) -> dict[str, object]:
    from omnimarket.nodes.node_projection_delegation.handlers.handler_projection_delegation import (
        HandlerProjectionDelegation,
    )

    captured: list[dict[str, object]] = []

    class _RecordingDB:
        def upsert(self, table: str, conflict_key: str, row: dict[str, object]) -> bool:
            captured.append(row)
            return True

        def query(
            self, table: str, filters: dict[str, object] | None = None
        ) -> list[dict[str, object]]:
            return []

    payload: dict[str, object] = {
        "_db": _RecordingDB(),
        "_event_type": "onex.evt.omnimarket.node-generation-completed.v1",
        "correlation_id": "gen-semantic-001",
        "task_description": "snake_case to PascalCase",
        "provider": "local",
        "model_id": "qwen3-coder",
        "endpoint_class": "local-coder",
        "attempt_count": 1,
        "total_latency_e2e_ms": 1234,
        "contract_passed": True,
        "cost_inference_usd": 0.0,
        "contract_yaml": "name: node_x\n",
        "handler_source": "def handle(input_data):\n    return {}\n",
        "routing_source": "contract",
        "resolved_endpoint": "http://host:8000/v1/chat/completions",
    }
    payload.update(payload_extra)
    HandlerProjectionDelegation().handle(payload)
    assert captured, "expected one generation_events upsert"
    return captured[0]


def test_sync_path_records_semantic_failure_distinct_from_contract_pass() -> None:
    """The gate-zero case: contract_passed=true but semantic_passed=false."""
    row = _project_generation({"semantic_checked": True, "semantic_passed": False})
    assert row["contract_passed"] is True
    assert row["semantic_checked"] is True
    assert row["semantic_passed"] is False


def test_sync_path_records_semantic_success() -> None:
    row = _project_generation({"semantic_checked": True, "semantic_passed": True})
    assert row["semantic_checked"] is True
    assert row["semantic_passed"] is True


def test_sync_path_defaults_semantic_fields_false_when_absent() -> None:
    """Legacy events without the fields default to behavior-not-verified."""
    row = _project_generation({})
    assert row["semantic_checked"] is False
    assert row["semantic_passed"] is False


# ---------------------------------------------------------------------------
# 4. Async runner write path emits both columns
# ---------------------------------------------------------------------------


def test_async_insert_includes_semantic_columns() -> None:
    source = ASYNC_HANDLER_PATH.read_text()
    for column in SEMANTIC_COLUMNS:
        assert column in source, (
            f"async _project_generation_completed INSERT must include {column}"
        )
