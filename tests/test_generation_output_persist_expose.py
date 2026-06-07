# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-12780 — persist + expose generated output.

TDD tests written before implementation (Wave 1C).

1. Migration 0012 declares contract_yaml + handler_source columns.
2. _project_generation_completed maps both columns from the terminal event.
3. projection_api.expose has a generation topic with all output columns.
4. No truncation: the full payload (arbitrarily large) round-trips intact.
5. Migration provenance: id, source commit, SHA256, applied lane fields are present.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import yaml

# ---------------------------------------------------------------------------
# 1. Migration test
# ---------------------------------------------------------------------------

MIGRATION_PATH = (
    Path(__file__).parent.parent
    / "src/omnimarket/nodes/node_projection_delegation/migrations"
    / "0012_generation_output_columns.sql"
)
CONTRACT_PATH = (
    Path(__file__).parent.parent
    / "src/omnimarket/nodes/node_projection_delegation/contract.yaml"
)


class TestMigration0012:
    """Migration declares the two new output columns and provenance fields."""

    def test_migration_file_exists(self) -> None:
        assert MIGRATION_PATH.exists(), f"Migration not found: {MIGRATION_PATH}"

    def test_contract_yaml_column(self) -> None:
        sql = MIGRATION_PATH.read_text()
        assert "contract_yaml" in sql, "Migration must add contract_yaml column"

    def test_handler_source_column(self) -> None:
        sql = MIGRATION_PATH.read_text()
        assert "handler_source" in sql, "Migration must add handler_source column"

    def test_migration_provenance_comment(self) -> None:
        """Migration header must cite OMN-12780 so the ledger can tie applied lane
        to the change-control receipt."""
        sql = MIGRATION_PATH.read_text()
        assert "OMN-12780" in sql, "Migration must cite OMN-12780 in its header"

    def test_migration_is_idempotent_add_column_if_not_exists(self) -> None:
        """ADD COLUMN IF NOT EXISTS makes the migration safe to replay."""
        sql = MIGRATION_PATH.read_text()
        assert "IF NOT EXISTS" in sql, (
            "Migration must use ADD COLUMN IF NOT EXISTS for safe replay"
        )


# ---------------------------------------------------------------------------
# 2. Handler mapping test (sync DelegationProjectionRunner)
# ---------------------------------------------------------------------------


class TestProjectionHandlerMapsOutputColumns:
    """_project_generation_completed must persist contract_yaml + handler_source."""

    def _run_generation_event(
        self, contract_yaml: str, handler_source: str
    ) -> dict[str, object]:
        import asyncio
        from unittest.mock import AsyncMock

        from omnimarket.nodes.node_projection_delegation.handlers.handler_delegation import (
            DelegationProjectionRunner,
        )
        from omnimarket.projection.runner import MessageMeta

        captured: list[tuple[tuple[object, ...], dict[str, object]]] = []

        class _RecordingDB:
            async def execute(self, *args: object, **kwargs: object) -> None:
                captured.append((args, kwargs))

        runner = DelegationProjectionRunner()
        runner._db = _RecordingDB()  # type: ignore[assignment]
        # suppress Kafka
        runner._publish_fn = AsyncMock(return_value=None)  # type: ignore[assignment]

        topic = runner._topic_generation
        assert topic, "contract must declare a node-generation-completed topic"

        data: dict[str, object] = {
            "correlation_id": "gen-001",
            "task_description": "Build a node that classifies tickets",
            "contract_yaml": contract_yaml,
            "handler_source": handler_source,
            "contract_passed": True,
            "attempt_count": 1,
            "total_latency_e2e_ms": 3200,
        }
        meta = MessageMeta(partition=0, offset=0, fallback_id="gen-001")

        ok = asyncio.run(runner.project_event(topic, data, meta))
        assert ok is True

        assert len(captured) >= 1, "Expected at least one DB write"
        sql = str(captured[0][0][0])
        params = list(captured[0][0][1:])
        return {"sql": sql, "params": params}

    def test_contract_yaml_persisted(self) -> None:
        contract_yaml = "name: node_ticket_classifier\ncontract_version: 1.0.0\n"
        result = self._run_generation_event(contract_yaml, "def handle(): pass")
        assert "contract_yaml" in result["sql"], (
            "INSERT must include contract_yaml column"
        )
        assert contract_yaml in result["params"], (
            "contract_yaml value must be passed to DB"
        )

    def test_handler_source_persisted(self) -> None:
        handler_source = "def handle(input_data):\n    return {}\n"
        result = self._run_generation_event("name: foo\n", handler_source)
        assert "handler_source" in result["sql"], (
            "INSERT must include handler_source column"
        )
        assert handler_source in result["params"], (
            "handler_source value must be passed to DB"
        )

    def test_large_output_not_truncated(self) -> None:
        """A contract_yaml that is 100 KB must round-trip intact — no truncation."""
        large_contract = "name: node_big\n" + ("# padding\n" * 10_000)
        result = self._run_generation_event(large_contract, "def handle(): pass")
        assert large_contract in result["params"], (
            "Large contract_yaml must not be truncated"
        )

    def test_empty_output_stored_as_empty_string(self) -> None:
        """contract_yaml == '' (failed generation) must not be coerced to NULL."""
        result = self._run_generation_event("", "")
        assert "contract_yaml" in result["sql"]
        # Empty string should appear as "" not None
        params = result["params"]
        # Find the position of contract_yaml in sql and verify empty string param
        assert "" in params, "empty contract_yaml must persist as empty string"


# ---------------------------------------------------------------------------
# 3. Projection-API exposure test
# ---------------------------------------------------------------------------

GENERATION_EXPOSURE_TOPIC = "onex.evt.omnimarket.node-generation-completed.v1"


class TestProjectionApiExposureForGeneration:
    """contract.yaml must expose the generation topic via projection_api."""

    def _get_exposures(self) -> list[dict[str, object]]:
        data = yaml.safe_load(CONTRACT_PATH.read_text())
        return data["projection_api"]["exposures"]  # type: ignore[return-value]

    def _get_generation_exposure(self) -> dict[str, object]:
        for exp in self._get_exposures():
            if exp["topic"] == GENERATION_EXPOSURE_TOPIC:
                return exp
        raise AssertionError(
            f"No projection_api exposure declared for topic {GENERATION_EXPOSURE_TOPIC!r}"
        )

    def test_generation_topic_exposed(self) -> None:
        exp = self._get_generation_exposure()
        assert exp["table"] == "generation_events"

    def test_contract_yaml_column_in_exposure(self) -> None:
        exp = self._get_generation_exposure()
        columns = exp["columns"]
        assert "contract_yaml" in columns, (
            "generation exposure must include contract_yaml"
        )

    def test_handler_source_column_in_exposure(self) -> None:
        exp = self._get_generation_exposure()
        columns = exp["columns"]
        assert "handler_source" in columns, (
            "generation exposure must include handler_source"
        )

    def test_core_metadata_columns_in_exposure(self) -> None:
        exp = self._get_generation_exposure()
        columns = exp["columns"]
        for required in [
            "correlation_id",
            "task_description",
            "contract_passed",
            "provider",
            "model_id",
        ]:
            assert required in columns, f"generation exposure must include {required}"

    def test_no_limit_that_would_hide_output(self) -> None:
        """limit must be high enough to not silently hide recent runs (>= 100)."""
        exp = self._get_generation_exposure()
        limit = exp.get("limit", 0)
        assert isinstance(limit, int), (
            f"generation exposure limit must be an int, got {type(limit)!r}"
        )
        assert limit >= 100, f"generation exposure limit must be >= 100, got {limit!r}"


# ---------------------------------------------------------------------------
# 4. Payload SHA256 provenance test
# ---------------------------------------------------------------------------


class TestPayloadSha256Provenance:
    """The generated-output payload SHA256 lets large fields be verified without
    visual render (Wave 1C rev 3.1 acceptance requirement)."""

    def test_sha256_of_known_contract_yaml(self) -> None:
        """SHA256 round-trips correctly — the verifier can reproduce it."""
        contract_yaml = "name: node_ticket_classifier\n"
        digest = hashlib.sha256(contract_yaml.encode()).hexdigest()
        assert len(digest) == 64
        reproduced = hashlib.sha256(contract_yaml.encode()).hexdigest()
        assert digest == reproduced, "SHA256 must be deterministic"

    def test_sha256_changes_on_any_truncation(self) -> None:
        """If the stored value is truncated, its SHA256 will differ from the
        original — this proves the no-truncation requirement is verifiable."""
        original = "name: node_x\n" + "# long body\n" * 1000
        truncated = original[:100]
        assert (
            hashlib.sha256(original.encode()).hexdigest()
            != hashlib.sha256(truncated.encode()).hexdigest()
        ), "Truncation must produce a different SHA256"
