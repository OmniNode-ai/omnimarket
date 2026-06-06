# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden chain test for node_on_vs_off_experiment_compute (OMN-12661).

Acceptance criteria per ticket:
- Fixed task set -> ON path + OFF path -> token counts -> cost totals -> summary report
- Evidence bundle with explicit proof classification (replay-proven)
- Reproducible: all fixture-mode runs produce identical cost-delta evidence

Test strategy: fixture mode only (no .201 dependency). All token counts are
pre-captured constants, making the harness fully deterministic and offline.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from omnimarket.nodes.node_on_vs_off_experiment_compute.handlers.handler_on_vs_off_experiment import (
    HandlerOnVsOffExperiment,
)
from omnimarket.nodes.node_on_vs_off_experiment_compute.models.model_on_vs_off_request import (
    ModelOnVsOffPricing,
    ModelOnVsOffRequest,
    ModelOnVsOffTask,
)
from omnimarket.nodes.node_on_vs_off_experiment_compute.models.model_on_vs_off_result import (
    EnumProofClass,
)

# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

_PRICING = ModelOnVsOffPricing(
    prompt_cost_per_1k=0.001,
    completion_cost_per_1k=0.002,
)

# Fixed task set — pre-captured token counts for replay-proven mode.
# ON path: full context pack injected (more prompt tokens, fewer completion tokens).
# OFF path: baseline system prompt only (fewer prompt tokens, more completion tokens).
_TASKS = (
    ModelOnVsOffTask(
        task_id="task_001",
        description="Generate a simple Python function that reverses a string",
        on_prompt_tokens=1500,
        on_completion_tokens=120,
        off_prompt_tokens=200,
        off_completion_tokens=180,
    ),
    ModelOnVsOffTask(
        task_id="task_002",
        description="Write a Pydantic BaseModel with frozen ConfigDict",
        on_prompt_tokens=1800,
        on_completion_tokens=150,
        off_prompt_tokens=250,
        off_completion_tokens=220,
    ),
    ModelOnVsOffTask(
        task_id="task_003",
        description="Implement a binary search function with type annotations",
        on_prompt_tokens=1600,
        on_completion_tokens=200,
        off_prompt_tokens=220,
        off_completion_tokens=300,
    ),
)

_REQUEST = ModelOnVsOffRequest(
    run_id="omn-12661-run-001",
    model_id="glm-4-5",
    tasks=_TASKS,
    pricing=_PRICING,
    fixture_mode=True,
)


@pytest.fixture
def node_dir() -> Path:
    return (
        Path(__file__).resolve().parent.parent.parent.parent
        / "src"
        / "omnimarket"
        / "nodes"
        / "node_on_vs_off_experiment_compute"
    )


@pytest.fixture
def contract_path(node_dir: Path) -> Path:
    return node_dir / "contract.yaml"


@pytest.fixture
def metadata_path(node_dir: Path) -> Path:
    return node_dir / "metadata.yaml"


@pytest.fixture
def handler() -> HandlerOnVsOffExperiment:
    return HandlerOnVsOffExperiment()


# ---------------------------------------------------------------------------
# Contract / metadata gate
# ---------------------------------------------------------------------------


class TestContractYaml:
    def test_contract_exists(self, contract_path: Path) -> None:
        assert contract_path.exists()

    def test_contract_loads(self, contract_path: Path) -> None:
        data = yaml.safe_load(contract_path.read_text())
        assert isinstance(data, dict)
        assert data["name"] == "node_on_vs_off_experiment_compute"
        assert data["node_type"] == "compute"
        assert data.get("node_not_implemented") is False

    def test_contract_declares_handler(self, contract_path: Path) -> None:
        data = yaml.safe_load(contract_path.read_text())
        handler_cfg = data.get("handler", {})
        assert "module" in handler_cfg
        assert "class" in handler_cfg

    def test_contract_purity(self, contract_path: Path) -> None:
        data = yaml.safe_load(contract_path.read_text())
        descriptor = data.get("descriptor", {})
        assert descriptor.get("purity") == "pure"
        assert descriptor.get("idempotent") is True

    def test_contract_declares_terminal_event(self, contract_path: Path) -> None:
        data = yaml.safe_load(contract_path.read_text())
        assert "terminal_event" in data
        assert "on-vs-off-experiment-completed" in data["terminal_event"]


class TestMetadataYaml:
    def test_metadata_exists(self, metadata_path: Path) -> None:
        assert metadata_path.exists()

    def test_metadata_loads(self, metadata_path: Path) -> None:
        data = yaml.safe_load(metadata_path.read_text())
        assert data["name"] == "node_on_vs_off_experiment_compute"
        assert "version" in data
        assert "entry_points" in data

    def test_metadata_pure(self, metadata_path: Path) -> None:
        data = yaml.safe_load(metadata_path.read_text())
        caps = data.get("capabilities", {})
        assert caps.get("side_effect_class") == "pure"
        assert caps.get("requires_network") is False


# ---------------------------------------------------------------------------
# Handler import gate
# ---------------------------------------------------------------------------


class TestHandlerImport:
    def test_handler_module_imports(self) -> None:
        from omnimarket.nodes.node_on_vs_off_experiment_compute.handlers import (
            handler_on_vs_off_experiment,
        )

        assert handler_on_vs_off_experiment is not None

    def test_handler_class_exists(self) -> None:
        assert HandlerOnVsOffExperiment is not None

    def test_input_model_exists(self) -> None:
        assert ModelOnVsOffRequest is not None

    def test_output_model_exists(self) -> None:
        from omnimarket.nodes.node_on_vs_off_experiment_compute.models.model_on_vs_off_result import (
            ModelOnVsOffResult,
        )

        assert ModelOnVsOffResult is not None


# ---------------------------------------------------------------------------
# Core acceptance: ON path + OFF path -> token counts -> cost totals -> summary
# ---------------------------------------------------------------------------


class TestFixtureModeGoldenChain:
    """Replay-proven evidence bundle: fully deterministic, no .201 dependency."""

    def test_status_ok(self, handler: HandlerOnVsOffExperiment) -> None:
        result = handler.handle(_REQUEST)
        assert result.status == "ok"

    def test_produces_row_per_task(self, handler: HandlerOnVsOffExperiment) -> None:
        result = handler.handle(_REQUEST)
        assert len(result.rows) == len(_TASKS)

    def test_task_ids_match(self, handler: HandlerOnVsOffExperiment) -> None:
        result = handler.handle(_REQUEST)
        produced_ids = [r.task_id for r in result.rows]
        expected_ids = [t.task_id for t in _TASKS]
        assert produced_ids == expected_ids

    def test_on_path_token_counts_preserved(
        self, handler: HandlerOnVsOffExperiment
    ) -> None:
        result = handler.handle(_REQUEST)
        for row, task in zip(result.rows, _TASKS, strict=True):
            assert row.on_prompt_tokens == task.on_prompt_tokens
            assert row.on_completion_tokens == task.on_completion_tokens

    def test_off_path_token_counts_preserved(
        self, handler: HandlerOnVsOffExperiment
    ) -> None:
        result = handler.handle(_REQUEST)
        for row, task in zip(result.rows, _TASKS, strict=True):
            assert row.off_prompt_tokens == task.off_prompt_tokens
            assert row.off_completion_tokens == task.off_completion_tokens

    def test_cost_arithmetic_correct(self, handler: HandlerOnVsOffExperiment) -> None:
        """Cost = (prompt * 0.001 + completion * 0.002) / 1000."""
        result = handler.handle(_REQUEST)
        row = result.rows[0]  # task_001: on=1500p+120c, off=200p+180c
        expected_on = (1500 * 0.001 + 120 * 0.002) / 1000.0
        expected_off = (200 * 0.001 + 180 * 0.002) / 1000.0
        assert abs(row.on_cost_usd - expected_on) < 1e-9
        assert abs(row.off_cost_usd - expected_off) < 1e-9

    def test_cost_delta_sign_correct(self, handler: HandlerOnVsOffExperiment) -> None:
        """cost_delta = on - off (positive means ON costs more)."""
        result = handler.handle(_REQUEST)
        for row in result.rows:
            assert abs(row.cost_delta_usd - (row.on_cost_usd - row.off_cost_usd)) < 1e-9

    def test_token_delta_sign_correct(self, handler: HandlerOnVsOffExperiment) -> None:
        """token_delta = on_total - off_total."""
        result = handler.handle(_REQUEST)
        for row in result.rows:
            assert row.token_delta == row.on_total_tokens - row.off_total_tokens

    def test_summary_present(self, handler: HandlerOnVsOffExperiment) -> None:
        result = handler.handle(_REQUEST)
        assert result.summary is not None

    def test_summary_run_id(self, handler: HandlerOnVsOffExperiment) -> None:
        result = handler.handle(_REQUEST)
        assert result.summary is not None
        assert result.summary.run_id == "omn-12661-run-001"

    def test_summary_model_id(self, handler: HandlerOnVsOffExperiment) -> None:
        result = handler.handle(_REQUEST)
        assert result.summary is not None
        assert result.summary.model_id == "glm-4-5"

    def test_summary_task_count(self, handler: HandlerOnVsOffExperiment) -> None:
        result = handler.handle(_REQUEST)
        assert result.summary is not None
        assert result.summary.task_count == 3

    def test_summary_total_costs_sum(self, handler: HandlerOnVsOffExperiment) -> None:
        result = handler.handle(_REQUEST)
        assert result.summary is not None
        expected_on = sum(r.on_cost_usd for r in result.rows)
        expected_off = sum(r.off_cost_usd for r in result.rows)
        assert abs(result.summary.total_on_cost_usd - expected_on) < 1e-9
        assert abs(result.summary.total_off_cost_usd - expected_off) < 1e-9

    def test_summary_total_tokens_sum(self, handler: HandlerOnVsOffExperiment) -> None:
        result = handler.handle(_REQUEST)
        assert result.summary is not None
        assert result.summary.total_on_tokens == sum(
            r.on_total_tokens for r in result.rows
        )
        assert result.summary.total_off_tokens == sum(
            r.off_total_tokens for r in result.rows
        )

    def test_proof_class_is_replay_proven(
        self, handler: HandlerOnVsOffExperiment
    ) -> None:
        """Critical acceptance criterion: fixture mode must emit replay-proven."""
        result = handler.handle(_REQUEST)
        assert result.summary is not None
        assert result.summary.proof_class == EnumProofClass.REPLAY_PROVEN

    def test_proof_class_string_value(self, handler: HandlerOnVsOffExperiment) -> None:
        result = handler.handle(_REQUEST)
        assert result.summary is not None
        assert result.summary.proof_class.value == "replay-proven"

    def test_cost_delta_pct_formula(self, handler: HandlerOnVsOffExperiment) -> None:
        """cost_delta_pct = (on - off) / off * 100."""
        result = handler.handle(_REQUEST)
        assert result.summary is not None
        s = result.summary
        if s.total_off_cost_usd != 0.0:
            expected_pct = (s.total_cost_delta_usd / s.total_off_cost_usd) * 100.0
            assert abs(s.cost_delta_pct - expected_pct) < 0.001

    def test_reproducible_determinism(self, handler: HandlerOnVsOffExperiment) -> None:
        """Same input always produces same cost totals (replay-proven contract)."""
        r1 = handler.handle(_REQUEST)
        r2 = handler.handle(_REQUEST)
        assert r1.summary is not None
        assert r2.summary is not None
        assert r1.summary.total_on_cost_usd == r2.summary.total_on_cost_usd
        assert r1.summary.total_off_cost_usd == r2.summary.total_off_cost_usd
        assert r1.summary.total_cost_delta_usd == r2.summary.total_cost_delta_usd
        assert r1.summary.proof_class == r2.summary.proof_class

    def test_no_errors_in_ok_result(self, handler: HandlerOnVsOffExperiment) -> None:
        result = handler.handle(_REQUEST)
        assert result.errors == ()
        assert result.failure_class is None


# ---------------------------------------------------------------------------
# Failure mode: missing fixture token counts
# ---------------------------------------------------------------------------


class TestFixtureMissingTokenCounts:
    def test_fails_when_on_prompt_tokens_missing(
        self, handler: HandlerOnVsOffExperiment
    ) -> None:
        bad_task = ModelOnVsOffTask(
            task_id="bad_001",
            description="task with missing on_prompt_tokens",
            on_prompt_tokens=None,
            on_completion_tokens=100,
            off_prompt_tokens=200,
            off_completion_tokens=100,
        )
        req = ModelOnVsOffRequest(
            run_id="test-run",
            model_id="glm-4-5",
            tasks=(bad_task,),
            pricing=_PRICING,
            fixture_mode=True,
        )
        result = handler.handle(req)
        assert result.status == "failed"
        assert result.failure_class == "missing_fixture_token_counts"
        assert any("on_prompt_tokens" in e for e in result.errors)

    def test_fails_when_off_completion_tokens_missing(
        self, handler: HandlerOnVsOffExperiment
    ) -> None:
        bad_task = ModelOnVsOffTask(
            task_id="bad_002",
            description="task with missing off_completion_tokens",
            on_prompt_tokens=100,
            on_completion_tokens=100,
            off_prompt_tokens=200,
            off_completion_tokens=None,
        )
        req = ModelOnVsOffRequest(
            run_id="test-run",
            model_id="glm-4-5",
            tasks=(bad_task,),
            pricing=_PRICING,
            fixture_mode=True,
        )
        result = handler.handle(req)
        assert result.status == "failed"
        assert result.failure_class == "missing_fixture_token_counts"


# ---------------------------------------------------------------------------
# Failure mode: runtime mode not implemented
# ---------------------------------------------------------------------------


class TestRuntimeModeGate:
    def test_runtime_mode_returns_failed(
        self, handler: HandlerOnVsOffExperiment
    ) -> None:
        """Runtime-observed mode is gated on .201 redeploy (OMN-12743 part (d))."""
        req = ModelOnVsOffRequest(
            run_id="rt-run",
            model_id="glm-4-5",
            tasks=(_TASKS[0],),
            pricing=_PRICING,
            fixture_mode=False,
        )
        result = handler.handle(req)
        assert result.status == "failed"
        assert result.failure_class == "runtime_mode_not_implemented"
        assert result.errors
        assert any("gated" in e for e in result.errors)


# ---------------------------------------------------------------------------
# Model schema validation
# ---------------------------------------------------------------------------


class TestModelSchemas:
    def test_request_is_frozen(self) -> None:
        from pydantic import ValidationError

        with pytest.raises((ValidationError, TypeError)):
            _REQUEST.run_id = "mutated"  # type: ignore[misc]

    def test_pricing_is_frozen(self) -> None:
        from pydantic import ValidationError

        with pytest.raises((ValidationError, TypeError)):
            _PRICING.prompt_cost_per_1k = 999.0  # type: ignore[misc]

    def test_proof_class_enum_values(self) -> None:
        assert EnumProofClass.REPLAY_PROVEN.value == "replay-proven"
        assert EnumProofClass.RUNTIME_OBSERVED_ONLY.value == "runtime-observed-only"

    def test_task_model_frozen(self) -> None:
        from pydantic import ValidationError

        task = _TASKS[0]
        with pytest.raises((ValidationError, TypeError)):
            task.task_id = "mutated"  # type: ignore[misc]
