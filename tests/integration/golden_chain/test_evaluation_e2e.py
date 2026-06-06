"""Golden chain e2e test: evaluation.

Chain: onex.evt.omniclaude.session-outcome.v1 -> session_outcomes

Validates that a session-outcome event is projected into the session_outcomes
table with session_id as the projection key.  Topics and table names are
read from golden_chains.yaml — never hardcoded.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from omnimarket.nodes.node_golden_chain_sweep.registry import load_registry
from omnimarket.nodes.node_projection_session_outcome.handlers.handler_session_outcome import (
    SessionOutcomeProjectionRunner,
)
from omnimarket.projection.runner import MessageMeta

_REGISTRY_PATH = (
    Path(__file__).parent.parent.parent.parent
    / "src/omnimarket/nodes/node_golden_chain_sweep/golden_chains.yaml"
)


def _load_evaluation_chain() -> tuple[str, str, list[str]]:
    """Return (head_topic, tail_table, expected_fields) for the evaluation chain."""
    chains = load_registry(path=_REGISTRY_PATH)
    for chain in chains:
        if chain.name == "evaluation":
            return chain.head_topic, chain.tail_table, list(chain.expected_fields)
    raise RuntimeError("evaluation chain not found in golden_chains.yaml")


@pytest.mark.unit
class TestEvaluationGoldenChainContract:
    """Contract-level guards: chain definition is well-formed and stable."""

    def test_evaluation_chain_exists_in_registry(self) -> None:
        chains = load_registry(path=_REGISTRY_PATH)
        names = [c.name for c in chains]
        assert "evaluation" in names, (
            f"evaluation chain missing from registry; found: {names}"
        )

    def test_evaluation_chain_head_topic(self) -> None:
        head_topic, _, _ = _load_evaluation_chain()
        assert head_topic == "onex.evt.omniclaude.session-outcome.v1"

    def test_evaluation_chain_tail_table(self) -> None:
        _, tail_table, _ = _load_evaluation_chain()
        assert tail_table == "session_outcomes"

    def test_evaluation_chain_expected_fields_contain_session_id(self) -> None:
        _, _, expected_fields = _load_evaluation_chain()
        assert "session_id" in expected_fields, (
            f"expected_fields must include session_id; got: {expected_fields}"
        )


@pytest.mark.unit
class TestEvaluationProjection:
    """Projection: session-outcome payload -> session_outcomes row."""

    def _make_runner(
        self,
    ) -> tuple[SessionOutcomeProjectionRunner, list[tuple[str, bytes]]]:
        published: list[tuple[str, bytes]] = []

        async def capture(topic: str, value: bytes) -> None:
            published.append((topic, value))

        from unittest.mock import AsyncMock, MagicMock

        from omnimarket.adapters.asyncpg_adapter import AsyncpgAdapter

        runner = SessionOutcomeProjectionRunner()
        mock_db = MagicMock(spec=AsyncpgAdapter)
        mock_db.execute = AsyncMock(return_value=None)
        runner._db = mock_db
        runner._publish_fn = capture  # type: ignore[attr-defined]
        return runner, published

    def test_run_evaluated_event_projects_session_id_from_correlation_id(self) -> None:
        head_topic, _, _ = _load_evaluation_chain()
        runner, _ = self._make_runner()

        data = {
            "correlation_id": "eval-corr-001",
            "run_id": "run-abc-001",
            "session_id": "sess-eval-001",
            "outcome": "success",
            "passed": True,
            "evaluated_at_utc": "2026-05-22T10:00:00Z",
        }
        meta = MessageMeta(partition=0, offset=0, fallback_id="eval-corr-001")

        ok = asyncio.run(runner.project_event(head_topic, data, meta))

        assert ok is True

    def test_run_evaluated_uses_session_id_as_projection_key(self) -> None:
        """session_id field takes priority over correlation_id as the UPSERT key."""
        head_topic, _, _ = _load_evaluation_chain()
        runner, _ = self._make_runner()

        data = {
            "session_id": "sess-explicit-001",
            "correlation_id": "eval-corr-002",
            "run_id": "run-xyz-002",
            "outcome": "failed",
            "passed": False,
            "evaluated_at_utc": "2026-05-22T11:00:00Z",
        }
        meta = MessageMeta(partition=0, offset=1, fallback_id="eval-corr-002")

        ok = asyncio.run(runner.project_event(head_topic, data, meta))

        assert ok is True

    def test_run_evaluated_falls_back_to_correlation_id_when_no_session_id(
        self,
    ) -> None:
        """correlation_id is used as session key when session_id is absent."""
        head_topic, _, _ = _load_evaluation_chain()
        runner, _ = self._make_runner()

        data = {
            "correlation_id": "eval-corr-003",
            "run_id": "run-fallback-003",
            "outcome": "unknown",
            "passed": False,
            "evaluated_at_utc": "2026-05-22T12:00:00Z",
        }
        meta = MessageMeta(partition=0, offset=2, fallback_id="eval-corr-003")

        ok = asyncio.run(runner.project_event(head_topic, data, meta))

        assert ok is True

    def test_event_with_no_session_id_and_no_correlation_id_is_skipped(self) -> None:
        """Events without a session key are skipped without error (returns True)."""
        head_topic, _, _ = _load_evaluation_chain()
        runner, _ = self._make_runner()

        data = {
            "run_id": "run-no-key",
            "outcome": "success",
            "passed": True,
            "evaluated_at_utc": "2026-05-22T13:00:00Z",
        }
        meta = MessageMeta(partition=0, offset=3, fallback_id="")

        ok = asyncio.run(runner.project_event(head_topic, data, meta))

        assert ok is True


@pytest.mark.unit
class TestEvaluationGoldenChainSweepIntegration:
    """Sweep handler validates evaluation chain against projected rows."""

    def test_sweep_passes_when_row_has_session_id(self) -> None:
        from omnimarket.nodes.node_golden_chain_sweep.handlers.handler_golden_chain_sweep import (
            EnumSweepStatus,
            GoldenChainSweepRequest,
            ModelChainDefinition,
            NodeGoldenChainSweep,
        )

        head_topic, tail_table, expected_fields = _load_evaluation_chain()
        handler = NodeGoldenChainSweep()
        request = GoldenChainSweepRequest(
            chains=[
                ModelChainDefinition(
                    name="evaluation",
                    head_topic=head_topic,
                    tail_table=tail_table,
                    expected_fields=expected_fields,
                )
            ],
            projected_rows={
                "evaluation": {
                    "session_id": "sess-sweep-001",
                    "outcome": "success",
                }
            },
        )
        result = handler.handle(request)

        assert result.overall_status == EnumSweepStatus.PASS
        assert result.chains_passed == 1
        assert result.chains_failed == 0

    def test_sweep_fails_when_session_id_missing_from_row(self) -> None:
        from omnimarket.nodes.node_golden_chain_sweep.handlers.handler_golden_chain_sweep import (
            EnumChainStatus,
            EnumSweepStatus,
            GoldenChainSweepRequest,
            ModelChainDefinition,
            NodeGoldenChainSweep,
        )

        head_topic, tail_table, expected_fields = _load_evaluation_chain()
        handler = NodeGoldenChainSweep()
        request = GoldenChainSweepRequest(
            chains=[
                ModelChainDefinition(
                    name="evaluation",
                    head_topic=head_topic,
                    tail_table=tail_table,
                    expected_fields=expected_fields,
                )
            ],
            projected_rows={
                "evaluation": {
                    "outcome": "success",
                    # session_id intentionally absent
                }
            },
        )
        result = handler.handle(request)

        assert result.overall_status == EnumSweepStatus.FAIL
        assert result.chain_results[0].status == EnumChainStatus.FAIL
        assert "session_id" in result.chain_results[0].missing_fields

    def test_sweep_times_out_when_no_row_projected(self) -> None:
        from omnimarket.nodes.node_golden_chain_sweep.handlers.handler_golden_chain_sweep import (
            EnumChainStatus,
            GoldenChainSweepRequest,
            ModelChainDefinition,
            NodeGoldenChainSweep,
        )

        head_topic, tail_table, expected_fields = _load_evaluation_chain()
        handler = NodeGoldenChainSweep()
        request = GoldenChainSweepRequest(
            chains=[
                ModelChainDefinition(
                    name="evaluation",
                    head_topic=head_topic,
                    tail_table=tail_table,
                    expected_fields=expected_fields,
                )
            ],
            projected_rows={},
        )
        result = handler.handle(request)

        assert result.chain_results[0].status == EnumChainStatus.TIMEOUT
