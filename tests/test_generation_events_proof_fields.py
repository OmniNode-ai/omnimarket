# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-12775 — populate generation_events proof fields in the write path.

The omnidash render reads six proof fields directly off the generation_events
row (``output_payload_sha256``, ``contract_sha256``, ``handler_sha256``,
``routing_source``, ``resolved_endpoint``, ``projection_owner``). Nothing
produced them, so the canonical projection API returned NULLs. The audit
(wwmx7fobe/D6) decision is to populate them in the write path because they ARE
the evidence the demo acceptance criteria require.

TDD tests, written from the contract acceptance criteria before implementation:

1. Migration 0013 declares all six proof columns (idempotent ADD COLUMN).
2. The canonical sync write path (HandlerProjectionDelegation, the live runtime
   path per OMN-12800) populates all six proof fields on the upserted row.
3. The async runner write path (DelegationProjectionRunner) populates the same
   six proof fields so the two writers stay in sync.
4. The SHA256 proof fields are the deterministic digests of the FULL payload —
   no truncation; a truncated value would yield a different digest.
5. contract.yaml exposes the six proof columns on the generation_events
   exposure so the canonical projection API returns them.
6. The terminal event model carries routing_source + resolved_endpoint so the
   projection can persist what the routing authority actually resolved.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import yaml

_MIGRATIONS_DIR = (
    Path(__file__).parent.parent
    / "src/omnimarket/nodes/node_projection_delegation/migrations"
)
MIGRATION_PATH = _MIGRATIONS_DIR / "0013_generation_proof_fields.sql"
CONTRACT_PATH = (
    Path(__file__).parent.parent
    / "src/omnimarket/nodes/node_projection_delegation/contract.yaml"
)

PROOF_COLUMNS = (
    "output_payload_sha256",
    "contract_sha256",
    "handler_sha256",
    "routing_source",
    "resolved_endpoint",
    "projection_owner",
)

# The canonical owner of the generation_events projection (the node that writes
# it). The dashboard renders this value instead of its own reader-fallback.
CANONICAL_PROJECTION_OWNER = "node_projection_delegation"


# ---------------------------------------------------------------------------
# 1. Migration declares all six proof columns
# ---------------------------------------------------------------------------


class TestMigration0013:
    def test_migration_file_exists(self) -> None:
        assert MIGRATION_PATH.exists(), f"Migration not found: {MIGRATION_PATH}"

    def test_all_proof_columns_declared(self) -> None:
        sql = MIGRATION_PATH.read_text()
        for column in PROOF_COLUMNS:
            assert column in sql, f"Migration must add {column} column"

    def test_migration_cites_ticket(self) -> None:
        sql = MIGRATION_PATH.read_text()
        assert "OMN-12775" in sql, "Migration must cite OMN-12775 in its header"

    def test_migration_is_idempotent(self) -> None:
        sql = MIGRATION_PATH.read_text()
        assert "IF NOT EXISTS" in sql, (
            "Migration must use ADD COLUMN IF NOT EXISTS for safe replay"
        )


# ---------------------------------------------------------------------------
# 2. Canonical sync write path populates the proof fields
# ---------------------------------------------------------------------------


class TestSyncWritePathPopulatesProofFields:
    """HandlerProjectionDelegation.project_generation_completed (live path)."""

    def _project(
        self,
        *,
        contract_yaml: str,
        handler_source: str,
        routing_source: str,
        resolved_endpoint: str,
    ) -> dict[str, object]:
        from omnimarket.nodes.node_projection_delegation.handlers.handler_projection_delegation import (
            HandlerProjectionDelegation,
        )

        captured: list[dict[str, object]] = []

        class _RecordingDB:
            def upsert(
                self, table: str, conflict_key: str, row: dict[str, object]
            ) -> bool:
                captured.append(row)
                return True

            def query(
                self, table: str, filters: dict[str, object] | None = None
            ) -> list[dict[str, object]]:
                return []

        handler = HandlerProjectionDelegation()
        payload: dict[str, object] = {
            "_db": _RecordingDB(),
            "_event_type": "onex.evt.omnimarket.node-generation-completed.v1",
            "correlation_id": "gen-proof-001",
            "task_description": "Build a classifier node",
            "provider": "local",
            "model_id": "qwen3-coder",
            "endpoint_class": "local-coder",
            "attempt_count": 1,
            "total_latency_e2e_ms": 1234,
            "contract_passed": True,
            "cost_inference_usd": 0.0,
            "contract_yaml": contract_yaml,
            "handler_source": handler_source,
            "routing_source": routing_source,
            "resolved_endpoint": resolved_endpoint,
        }
        handler.handle(payload)
        assert captured, "expected one generation_events upsert"
        return captured[0]

    def test_all_proof_fields_present_on_row(self) -> None:
        row = self._project(
            contract_yaml="name: node_x\n",
            handler_source="def handle(input_data):\n    return {}\n",
            routing_source="contract",
            resolved_endpoint="http://local-coder.example:8000/v1/chat/completions",
        )
        for column in PROOF_COLUMNS:
            assert column in row, f"upserted row must include {column}"

    def test_sha256_fields_are_digests_of_full_payload(self) -> None:
        contract_yaml = "name: node_big\n" + ("# pad\n" * 5_000)
        handler_source = "def handle(input_data):\n    return {}\n" + ("# x\n" * 5_000)
        row = self._project(
            contract_yaml=contract_yaml,
            handler_source=handler_source,
            routing_source="contract",
            resolved_endpoint="http://host:8000/v1/chat/completions",
        )
        combined = (contract_yaml + handler_source).encode()
        assert (
            row["contract_sha256"] == hashlib.sha256(contract_yaml.encode()).hexdigest()
        )
        assert (
            row["handler_sha256"] == hashlib.sha256(handler_source.encode()).hexdigest()
        )
        assert row["output_payload_sha256"] == hashlib.sha256(combined).hexdigest()

    def test_routing_source_and_resolved_endpoint_round_trip(self) -> None:
        row = self._project(
            contract_yaml="name: node_x\n",
            handler_source="def handle(input_data):\n    return {}\n",
            routing_source="routing_authority",
            resolved_endpoint="https://generativelanguage.googleapis.com/v1beta/openai/chat/completions",
        )
        assert row["routing_source"] == "routing_authority"
        assert (
            row["resolved_endpoint"]
            == "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions"
        )

    def test_projection_owner_is_canonical_node(self) -> None:
        row = self._project(
            contract_yaml="name: node_x\n",
            handler_source="def handle(input_data):\n    return {}\n",
            routing_source="contract",
            resolved_endpoint="http://host:8000/v1/chat/completions",
        )
        assert row["projection_owner"] == CANONICAL_PROJECTION_OWNER


# ---------------------------------------------------------------------------
# 3. Async runner write path populates the same proof fields
# ---------------------------------------------------------------------------


class TestAsyncRunnerPopulatesProofFields:
    def _run(
        self,
        *,
        contract_yaml: str,
        handler_source: str,
        routing_source: str,
        resolved_endpoint: str,
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
        runner._publish_fn = AsyncMock(return_value=None)  # type: ignore[assignment]

        topic = runner._topic_generation
        data: dict[str, object] = {
            "correlation_id": "gen-async-001",
            "task_description": "Build a node",
            "contract_yaml": contract_yaml,
            "handler_source": handler_source,
            "contract_passed": True,
            "attempt_count": 1,
            "total_latency_e2e_ms": 100,
            "routing_source": routing_source,
            "resolved_endpoint": resolved_endpoint,
        }
        meta = MessageMeta(partition=0, offset=0, fallback_id="gen-async-001")
        ok = asyncio.run(runner.project_event(topic, data, meta))
        assert ok is True
        assert captured, "expected one DB write"
        sql = str(captured[0][0][0])
        params = list(captured[0][0][1:])
        return {"sql": sql, "params": params}

    def test_proof_columns_in_insert(self) -> None:
        result = self._run(
            contract_yaml="name: node_x\n",
            handler_source="def handle(input_data):\n    return {}\n",
            routing_source="contract",
            resolved_endpoint="http://host:8000/v1/chat/completions",
        )
        sql = str(result["sql"])
        for column in PROOF_COLUMNS:
            assert column in sql, f"INSERT must include {column}"

    def test_sha256_values_passed_to_db(self) -> None:
        contract_yaml = "name: node_x\n"
        handler_source = "def handle(input_data):\n    return {}\n"
        result = self._run(
            contract_yaml=contract_yaml,
            handler_source=handler_source,
            routing_source="contract",
            resolved_endpoint="http://host:8000/v1/chat/completions",
        )
        params = result["params"]
        assert isinstance(params, list)
        assert hashlib.sha256(contract_yaml.encode()).hexdigest() in params
        assert hashlib.sha256(handler_source.encode()).hexdigest() in params

    def test_projection_owner_passed_to_db(self) -> None:
        result = self._run(
            contract_yaml="name: node_x\n",
            handler_source="def handle(input_data):\n    return {}\n",
            routing_source="contract",
            resolved_endpoint="http://host:8000/v1/chat/completions",
        )
        params = result["params"]
        assert isinstance(params, list)
        assert CANONICAL_PROJECTION_OWNER in params


# ---------------------------------------------------------------------------
# 4. contract.yaml exposes the proof columns on the canonical projection API
# ---------------------------------------------------------------------------

GENERATION_EXPOSURE_TOPIC = "onex.evt.omnimarket.node-generation-completed.v1"


class TestProjectionApiExposesProofColumns:
    def _get_generation_exposure(self) -> dict[str, object]:
        data = yaml.safe_load(CONTRACT_PATH.read_text())
        for exp in data["projection_api"]["exposures"]:
            if exp["topic"] == GENERATION_EXPOSURE_TOPIC:
                return exp  # type: ignore[no-any-return]
        raise AssertionError(
            f"No projection_api exposure for topic {GENERATION_EXPOSURE_TOPIC!r}"
        )

    def test_proof_columns_in_exposure(self) -> None:
        exp = self._get_generation_exposure()
        columns = exp["columns"]
        for column in PROOF_COLUMNS:
            assert column in columns, f"generation exposure must include {column}"


# ---------------------------------------------------------------------------
# 5. Terminal event model carries routing_source + resolved_endpoint
# ---------------------------------------------------------------------------


class TestTerminalEventModelCarriesRoutingProof:
    def test_benchmark_has_routing_proof_fields(self) -> None:
        from omnimarket.nodes.node_generation_consumer.models.model_generation import (
            ModelGenerationBenchmark,
        )

        benchmark = ModelGenerationBenchmark(
            correlation_id="c1",
            task_description="t",
            routing_source="contract",
            resolved_endpoint="http://host:8000/v1/chat/completions",
        )
        assert benchmark.routing_source == "contract"
        assert benchmark.resolved_endpoint == "http://host:8000/v1/chat/completions"

    def test_projection_event_model_has_routing_proof_fields(self) -> None:
        from omnimarket.nodes.node_projection_delegation.handlers.handler_projection_delegation import (
            ModelProjectionGenerationCompletedEvent,
        )

        event = ModelProjectionGenerationCompletedEvent(
            correlation_id="c1",
            routing_source="contract",
            resolved_endpoint="http://host:8000/v1/chat/completions",
        )
        assert event.routing_source == "contract"
        assert event.resolved_endpoint == "http://host:8000/v1/chat/completions"
