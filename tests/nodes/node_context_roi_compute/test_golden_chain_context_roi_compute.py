# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden chain test for node_context_roi_compute (OMN-12796).

N-arm COMPUTE scorer: given frozen rows (one per task x factor_subset x trial),
aggregates per-subset stats and computes deltas vs the `off` arm.

Acceptance criteria per ticket:
- Given frozen rows, emits an N-arm per-subset summary with deltas vs `off`
  and proof_class, matching the OMN-12661 scorer shape.
- HEADLINE metric: first_pass_success rate + cost-per-success (NOT mean-attempts).
- Per-subset: mean/median attempts, first-pass rate, final-pass rate,
  prompt/completion token deltas, cost delta and cost-per-success vs `off`,
  with variance.

Test strategy: fixture mode only (no .201 dependency). All rows are pre-captured
constants, making the scorer fully deterministic and offline-capable.
"""

from __future__ import annotations

import statistics
from pathlib import Path

import pytest
import yaml

from omnimarket.nodes.node_context_roi_compute.handlers.handler_context_roi_compute import (
    HandlerContextRoiCompute,
)
from omnimarket.nodes.node_context_roi_compute.models.model_context_roi_request import (
    ModelContextRoiPricing,
    ModelContextRoiRequest,
    ModelContextRoiRow,
)
from omnimarket.nodes.node_context_roi_compute.models.model_context_roi_result import (
    EnumProofClass,
)

# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

_PRICING = ModelContextRoiPricing(
    prompt_cost_per_1k=0.001,
    completion_cost_per_1k=0.002,
)

# Three factor subsets: off (baseline), golden_only, golden_exemplar.
# Each has 2 tasks x 3 trials = 6 rows per subset.
# off arm: higher attempt counts, lower first-pass rate, no context overhead.
# golden_only: moderate improvement.
# golden_exemplar: best improvement.

_OFF_ROWS = (
    # task_001 trials
    ModelContextRoiRow(
        run_id="run-001",
        task_id="task_001",
        factor_subset="off",
        trial_index=0,
        attempt_count=2,
        first_pass_success=False,
        final_success=True,
        prompt_tokens=200,
        completion_tokens=180,
        estimated_cost_usd=None,
    ),
    ModelContextRoiRow(
        run_id="run-001",
        task_id="task_001",
        factor_subset="off",
        trial_index=1,
        attempt_count=2,
        first_pass_success=False,
        final_success=True,
        prompt_tokens=210,
        completion_tokens=175,
        estimated_cost_usd=None,
    ),
    ModelContextRoiRow(
        run_id="run-001",
        task_id="task_001",
        factor_subset="off",
        trial_index=2,
        attempt_count=1,
        first_pass_success=True,
        final_success=True,
        prompt_tokens=195,
        completion_tokens=185,
        estimated_cost_usd=None,
    ),
    # task_002 trials
    ModelContextRoiRow(
        run_id="run-001",
        task_id="task_002",
        factor_subset="off",
        trial_index=0,
        attempt_count=2,
        first_pass_success=False,
        final_success=True,
        prompt_tokens=250,
        completion_tokens=220,
        estimated_cost_usd=None,
    ),
    ModelContextRoiRow(
        run_id="run-001",
        task_id="task_002",
        factor_subset="off",
        trial_index=1,
        attempt_count=2,
        first_pass_success=False,
        final_success=False,
        prompt_tokens=240,
        completion_tokens=230,
        estimated_cost_usd=None,
    ),
    ModelContextRoiRow(
        run_id="run-001",
        task_id="task_002",
        factor_subset="off",
        trial_index=2,
        attempt_count=1,
        first_pass_success=True,
        final_success=True,
        prompt_tokens=245,
        completion_tokens=210,
        estimated_cost_usd=None,
    ),
)

_GOLDEN_ONLY_ROWS = (
    ModelContextRoiRow(
        run_id="run-001",
        task_id="task_001",
        factor_subset="golden_only",
        trial_index=0,
        attempt_count=1,
        first_pass_success=True,
        final_success=True,
        prompt_tokens=1500,
        completion_tokens=120,
        estimated_cost_usd=None,
    ),
    ModelContextRoiRow(
        run_id="run-001",
        task_id="task_001",
        factor_subset="golden_only",
        trial_index=1,
        attempt_count=2,
        first_pass_success=False,
        final_success=True,
        prompt_tokens=1510,
        completion_tokens=130,
        estimated_cost_usd=None,
    ),
    ModelContextRoiRow(
        run_id="run-001",
        task_id="task_001",
        factor_subset="golden_only",
        trial_index=2,
        attempt_count=1,
        first_pass_success=True,
        final_success=True,
        prompt_tokens=1490,
        completion_tokens=125,
        estimated_cost_usd=None,
    ),
    ModelContextRoiRow(
        run_id="run-001",
        task_id="task_002",
        factor_subset="golden_only",
        trial_index=0,
        attempt_count=1,
        first_pass_success=True,
        final_success=True,
        prompt_tokens=1800,
        completion_tokens=150,
        estimated_cost_usd=None,
    ),
    ModelContextRoiRow(
        run_id="run-001",
        task_id="task_002",
        factor_subset="golden_only",
        trial_index=1,
        attempt_count=1,
        first_pass_success=True,
        final_success=True,
        prompt_tokens=1810,
        completion_tokens=155,
        estimated_cost_usd=None,
    ),
    ModelContextRoiRow(
        run_id="run-001",
        task_id="task_002",
        factor_subset="golden_only",
        trial_index=2,
        attempt_count=2,
        first_pass_success=False,
        final_success=True,
        prompt_tokens=1790,
        completion_tokens=160,
        estimated_cost_usd=None,
    ),
)

_GOLDEN_EXEMPLAR_ROWS = (
    ModelContextRoiRow(
        run_id="run-001",
        task_id="task_001",
        factor_subset="golden_exemplar",
        trial_index=0,
        attempt_count=1,
        first_pass_success=True,
        final_success=True,
        prompt_tokens=2000,
        completion_tokens=100,
        estimated_cost_usd=None,
    ),
    ModelContextRoiRow(
        run_id="run-001",
        task_id="task_001",
        factor_subset="golden_exemplar",
        trial_index=1,
        attempt_count=1,
        first_pass_success=True,
        final_success=True,
        prompt_tokens=2010,
        completion_tokens=105,
        estimated_cost_usd=None,
    ),
    ModelContextRoiRow(
        run_id="run-001",
        task_id="task_001",
        factor_subset="golden_exemplar",
        trial_index=2,
        attempt_count=1,
        first_pass_success=True,
        final_success=True,
        prompt_tokens=1990,
        completion_tokens=110,
        estimated_cost_usd=None,
    ),
    ModelContextRoiRow(
        run_id="run-001",
        task_id="task_002",
        factor_subset="golden_exemplar",
        trial_index=0,
        attempt_count=1,
        first_pass_success=True,
        final_success=True,
        prompt_tokens=2200,
        completion_tokens=130,
        estimated_cost_usd=None,
    ),
    ModelContextRoiRow(
        run_id="run-001",
        task_id="task_002",
        factor_subset="golden_exemplar",
        trial_index=1,
        attempt_count=1,
        first_pass_success=True,
        final_success=True,
        prompt_tokens=2210,
        completion_tokens=125,
        estimated_cost_usd=None,
    ),
    ModelContextRoiRow(
        run_id="run-001",
        task_id="task_002",
        factor_subset="golden_exemplar",
        trial_index=2,
        attempt_count=1,
        first_pass_success=True,
        final_success=True,
        prompt_tokens=2190,
        completion_tokens=135,
        estimated_cost_usd=None,
    ),
)

_ALL_ROWS = _OFF_ROWS + _GOLDEN_ONLY_ROWS + _GOLDEN_EXEMPLAR_ROWS

_REQUEST = ModelContextRoiRequest(
    run_id="omn-12796-run-001",
    model_id="glm-4-5",
    rows=_ALL_ROWS,
    pricing=_PRICING,
    off_arm_label="off",
    fixture_mode=True,
)


@pytest.fixture
def node_dir() -> Path:
    return (
        Path(__file__).resolve().parent.parent.parent.parent
        / "src"
        / "omnimarket"
        / "nodes"
        / "node_context_roi_compute"
    )


@pytest.fixture
def contract_path(node_dir: Path) -> Path:
    return node_dir / "contract.yaml"


@pytest.fixture
def metadata_path(node_dir: Path) -> Path:
    return node_dir / "metadata.yaml"


@pytest.fixture
def handler() -> HandlerContextRoiCompute:
    return HandlerContextRoiCompute()


# ---------------------------------------------------------------------------
# Contract / metadata gate
# ---------------------------------------------------------------------------


class TestContractYaml:
    def test_contract_exists(self, contract_path: Path) -> None:
        assert contract_path.exists()

    def test_contract_loads(self, contract_path: Path) -> None:
        data = yaml.safe_load(contract_path.read_text())
        assert isinstance(data, dict)
        assert data["name"] == "node_context_roi_compute"
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
        assert "context-roi-score-completed" in data["terminal_event"]

    def test_contract_subscribe_topic(self, contract_path: Path) -> None:
        data = yaml.safe_load(contract_path.read_text())
        subscribe = data.get("event_bus", {}).get("subscribe_topics", [])
        assert "onex.cmd.omnimarket.context-roi-score-requested.v1" in subscribe

    def test_contract_publish_topics(self, contract_path: Path) -> None:
        data = yaml.safe_load(contract_path.read_text())
        publish = data.get("event_bus", {}).get("publish_topics", [])
        assert "onex.evt.omnimarket.context-roi-score-completed.v1" in publish
        assert "onex.evt.omnimarket.context-roi-score-failed.v1" in publish


class TestMetadataYaml:
    def test_metadata_exists(self, metadata_path: Path) -> None:
        assert metadata_path.exists()

    def test_metadata_loads(self, metadata_path: Path) -> None:
        data = yaml.safe_load(metadata_path.read_text())
        assert data["name"] == "node_context_roi_compute"
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
        from omnimarket.nodes.node_context_roi_compute.handlers import (
            handler_context_roi_compute,
        )

        assert handler_context_roi_compute is not None

    def test_handler_class_exists(self) -> None:
        assert HandlerContextRoiCompute is not None

    def test_input_model_exists(self) -> None:
        assert ModelContextRoiRequest is not None

    def test_output_model_exists(self) -> None:
        from omnimarket.nodes.node_context_roi_compute.models.model_context_roi_result import (
            ModelContextRoiResult,
        )

        assert ModelContextRoiResult is not None


# ---------------------------------------------------------------------------
# Core acceptance: N-arm per-subset aggregation with deltas vs off
# ---------------------------------------------------------------------------


class TestFixtureModeGoldenChain:
    """Replay-proven N-arm evidence bundle: deterministic, no .201 dependency."""

    def test_status_ok(self, handler: HandlerContextRoiCompute) -> None:
        result = handler.handle(_REQUEST)
        assert result.status == "ok"

    def test_produces_summary_per_subset(
        self, handler: HandlerContextRoiCompute
    ) -> None:
        result = handler.handle(_REQUEST)
        assert result.subset_summaries is not None
        labels = {s.factor_subset for s in result.subset_summaries}
        assert "off" in labels
        assert "golden_only" in labels
        assert "golden_exemplar" in labels

    def test_off_arm_has_no_delta(self, handler: HandlerContextRoiCompute) -> None:
        """off arm deltas vs itself must be 0."""
        result = handler.handle(_REQUEST)
        assert result.subset_summaries is not None
        off = next(s for s in result.subset_summaries if s.factor_subset == "off")
        assert off.first_pass_rate_delta_vs_off == 0.0
        assert off.cost_per_success_delta_vs_off == 0.0

    def test_first_pass_rate_off_arm(self, handler: HandlerContextRoiCompute) -> None:
        """off arm: 2 first-pass successes out of 6 trials = 1/3."""
        result = handler.handle(_REQUEST)
        assert result.subset_summaries is not None
        off = next(s for s in result.subset_summaries if s.factor_subset == "off")
        # off rows first_pass_success: F,F,T,F,F,T → 2/6
        assert abs(off.first_pass_rate - 2 / 6) < 1e-9

    def test_final_pass_rate_off_arm(self, handler: HandlerContextRoiCompute) -> None:
        """off arm: 5 final successes out of 6 trials = 5/6."""
        result = handler.handle(_REQUEST)
        assert result.subset_summaries is not None
        off = next(s for s in result.subset_summaries if s.factor_subset == "off")
        # final_success: T,T,T,T,F,T → 5/6
        assert abs(off.final_pass_rate - 5 / 6) < 1e-9

    def test_first_pass_rate_golden_exemplar(
        self, handler: HandlerContextRoiCompute
    ) -> None:
        """golden_exemplar arm: all 6 trials are first-pass successes."""
        result = handler.handle(_REQUEST)
        assert result.subset_summaries is not None
        ge = next(
            s for s in result.subset_summaries if s.factor_subset == "golden_exemplar"
        )
        assert abs(ge.first_pass_rate - 1.0) < 1e-9

    def test_delta_vs_off_positive_for_improvement(
        self, handler: HandlerContextRoiCompute
    ) -> None:
        """golden_exemplar first_pass_rate_delta_vs_off should be positive (improvement)."""
        result = handler.handle(_REQUEST)
        assert result.subset_summaries is not None
        ge = next(
            s for s in result.subset_summaries if s.factor_subset == "golden_exemplar"
        )
        assert ge.first_pass_rate_delta_vs_off > 0.0

    def test_mean_attempts_off_arm(self, handler: HandlerContextRoiCompute) -> None:
        """off arm mean attempts: (2+2+1+2+2+1)/6 = 10/6."""
        result = handler.handle(_REQUEST)
        assert result.subset_summaries is not None
        off = next(s for s in result.subset_summaries if s.factor_subset == "off")
        expected = statistics.mean([2, 2, 1, 2, 2, 1])
        assert abs(off.mean_attempts - expected) < 1e-9

    def test_median_attempts_off_arm(self, handler: HandlerContextRoiCompute) -> None:
        """off arm median attempts: median of [1,1,2,2,2,2] = 2.0."""
        result = handler.handle(_REQUEST)
        assert result.subset_summaries is not None
        off = next(s for s in result.subset_summaries if s.factor_subset == "off")
        expected = statistics.median([2, 2, 1, 2, 2, 1])
        assert abs(off.median_attempts - expected) < 1e-9

    def test_cost_arithmetic_uses_pricing(
        self, handler: HandlerContextRoiCompute
    ) -> None:
        """cost = (prompt * 0.001 + completion * 0.002) / 1000 per row."""
        result = handler.handle(_REQUEST)
        assert result.subset_summaries is not None
        off = next(s for s in result.subset_summaries if s.factor_subset == "off")
        # off arm total cost: sum over 6 rows
        expected_costs = [
            (200 * 0.001 + 180 * 0.002) / 1000.0,
            (210 * 0.001 + 175 * 0.002) / 1000.0,
            (195 * 0.001 + 185 * 0.002) / 1000.0,
            (250 * 0.001 + 220 * 0.002) / 1000.0,
            (240 * 0.001 + 230 * 0.002) / 1000.0,
            (245 * 0.001 + 210 * 0.002) / 1000.0,
        ]
        expected_total = sum(expected_costs)
        assert abs(off.total_cost_usd - expected_total) < 1e-9

    def test_cost_per_success_off_arm(self, handler: HandlerContextRoiCompute) -> None:
        """cost_per_success = total_cost / successful_final_trials."""
        result = handler.handle(_REQUEST)
        assert result.subset_summaries is not None
        off = next(s for s in result.subset_summaries if s.factor_subset == "off")
        # 5 out of 6 trials final_success=True
        expected_costs = [
            (200 * 0.001 + 180 * 0.002) / 1000.0,
            (210 * 0.001 + 175 * 0.002) / 1000.0,
            (195 * 0.001 + 185 * 0.002) / 1000.0,
            (250 * 0.001 + 220 * 0.002) / 1000.0,
            (240 * 0.001 + 230 * 0.002) / 1000.0,
            (245 * 0.001 + 210 * 0.002) / 1000.0,
        ]
        total_cost = sum(expected_costs)
        expected_cps = total_cost / 5  # 5 successes
        assert abs(off.cost_per_success_usd - expected_cps) < 1e-9

    def test_prompt_token_delta_vs_off(self, handler: HandlerContextRoiCompute) -> None:
        """golden_only has higher prompt tokens (context injected); delta > 0."""
        result = handler.handle(_REQUEST)
        assert result.subset_summaries is not None
        go = next(
            s for s in result.subset_summaries if s.factor_subset == "golden_only"
        )
        # golden_only mean prompt tokens >> off mean prompt tokens
        assert go.mean_prompt_token_delta_vs_off > 0.0

    def test_completion_token_delta_direction(
        self, handler: HandlerContextRoiCompute
    ) -> None:
        """golden_only has fewer completion tokens (better guided); delta < 0."""
        result = handler.handle(_REQUEST)
        assert result.subset_summaries is not None
        go = next(
            s for s in result.subset_summaries if s.factor_subset == "golden_only"
        )
        assert go.mean_completion_token_delta_vs_off < 0.0

    def test_proof_class_is_replay_proven(
        self, handler: HandlerContextRoiCompute
    ) -> None:
        """fixture_mode=True must emit REPLAY_PROVEN."""
        result = handler.handle(_REQUEST)
        assert result.proof_class == EnumProofClass.REPLAY_PROVEN

    def test_reproducible_determinism(self, handler: HandlerContextRoiCompute) -> None:
        """Same input always produces identical per-subset summaries."""
        r1 = handler.handle(_REQUEST)
        r2 = handler.handle(_REQUEST)
        assert r1.proof_class == r2.proof_class
        assert r1.status == r2.status
        assert r1.subset_summaries is not None
        assert r2.subset_summaries is not None
        for s1, s2 in zip(
            sorted(r1.subset_summaries, key=lambda s: s.factor_subset),
            sorted(r2.subset_summaries, key=lambda s: s.factor_subset),
            strict=True,
        ):
            assert s1.first_pass_rate == s2.first_pass_rate
            assert s1.total_cost_usd == s2.total_cost_usd

    def test_no_errors_in_ok_result(self, handler: HandlerContextRoiCompute) -> None:
        result = handler.handle(_REQUEST)
        assert result.errors == ()
        assert result.failure_class is None

    def test_variance_present_in_summaries(
        self, handler: HandlerContextRoiCompute
    ) -> None:
        """Per-subset summaries carry attempt_count_variance."""
        result = handler.handle(_REQUEST)
        assert result.subset_summaries is not None
        for s in result.subset_summaries:
            assert s.attempt_count_variance >= 0.0

    def test_row_count_per_subset(self, handler: HandlerContextRoiCompute) -> None:
        """Each subset summary records the number of rows scored."""
        result = handler.handle(_REQUEST)
        assert result.subset_summaries is not None
        for s in result.subset_summaries:
            assert s.row_count == 6  # 2 tasks x 3 trials


# ---------------------------------------------------------------------------
# Headline metrics: first_pass_rate + cost_per_success are the primary signals
# ---------------------------------------------------------------------------


class TestHeadlineMetrics:
    def test_headline_metric_first_pass_rate_improves(
        self, handler: HandlerContextRoiCompute
    ) -> None:
        """golden_exemplar first_pass_rate > off first_pass_rate."""
        result = handler.handle(_REQUEST)
        assert result.subset_summaries is not None
        off = next(s for s in result.subset_summaries if s.factor_subset == "off")
        ge = next(
            s for s in result.subset_summaries if s.factor_subset == "golden_exemplar"
        )
        assert ge.first_pass_rate > off.first_pass_rate

    def test_cost_per_success_present_when_successes_exist(
        self, handler: HandlerContextRoiCompute
    ) -> None:
        """cost_per_success is non-None when there is at least one final success."""
        result = handler.handle(_REQUEST)
        assert result.subset_summaries is not None
        for s in result.subset_summaries:
            if s.final_pass_rate > 0.0:
                assert s.cost_per_success_usd is not None

    def test_cost_per_success_none_when_no_success(
        self, handler: HandlerContextRoiCompute
    ) -> None:
        """cost_per_success must be None (not infinity/0) when no trials succeed."""
        all_fail_rows = (
            ModelContextRoiRow(
                run_id="r",
                task_id="t1",
                factor_subset="off",
                trial_index=0,
                attempt_count=2,
                first_pass_success=False,
                final_success=False,
                prompt_tokens=100,
                completion_tokens=100,
                estimated_cost_usd=None,
            ),
        )
        req = ModelContextRoiRequest(
            run_id="test-zero-success",
            model_id="glm-4-5",
            rows=all_fail_rows,
            pricing=_PRICING,
            off_arm_label="off",
            fixture_mode=True,
        )
        result = HandlerContextRoiCompute().handle(req)
        assert result.status == "ok"
        assert result.subset_summaries is not None
        off = next(s for s in result.subset_summaries if s.factor_subset == "off")
        assert off.cost_per_success_usd is None
        assert off.cost_per_success_delta_vs_off is None


# ---------------------------------------------------------------------------
# Failure modes
# ---------------------------------------------------------------------------


class TestFailureModes:
    def test_runtime_mode_not_implemented(self) -> None:
        """fixture_mode=False returns failed with clear message."""
        req = ModelContextRoiRequest(
            run_id="rt-run",
            model_id="glm-4-5",
            rows=(_OFF_ROWS[0],),
            pricing=_PRICING,
            off_arm_label="off",
            fixture_mode=False,
        )
        result = HandlerContextRoiCompute().handle(req)
        assert result.status == "failed"
        assert result.failure_class == "runtime_mode_not_implemented"
        assert result.errors
        assert any("gated" in e for e in result.errors)

    def test_missing_off_arm_rows_fails(self) -> None:
        """If no rows belong to the off arm, scorer fails cleanly."""
        no_off_rows = (
            ModelContextRoiRow(
                run_id="r",
                task_id="t1",
                factor_subset="golden_only",
                trial_index=0,
                attempt_count=1,
                first_pass_success=True,
                final_success=True,
                prompt_tokens=1000,
                completion_tokens=100,
                estimated_cost_usd=None,
            ),
        )
        req = ModelContextRoiRequest(
            run_id="test-no-off",
            model_id="glm-4-5",
            rows=no_off_rows,
            pricing=_PRICING,
            off_arm_label="off",
            fixture_mode=True,
        )
        result = HandlerContextRoiCompute().handle(req)
        assert result.status == "failed"
        assert result.failure_class == "missing_off_arm"

    def test_empty_rows_fails(self) -> None:
        """Empty row tuple must fail, not produce empty summaries."""
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            ModelContextRoiRequest(
                run_id="empty",
                model_id="glm-4-5",
                rows=(),
                pricing=_PRICING,
                off_arm_label="off",
                fixture_mode=True,
            )


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

    def test_row_model_frozen(self) -> None:
        from pydantic import ValidationError

        row = _OFF_ROWS[0]
        with pytest.raises((ValidationError, TypeError)):
            row.task_id = "mutated"  # type: ignore[misc]
