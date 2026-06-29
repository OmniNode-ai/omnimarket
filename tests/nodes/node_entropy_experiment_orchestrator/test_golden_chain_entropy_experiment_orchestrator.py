# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden chain test for node_entropy_experiment_orchestrator (OMN-13614).

Phase 3.1 of the SEA->canonical migration (epic OMN-13604). This ORCHESTRATOR
absorbs the SEA entropy-comparison harness / failure-taxonomy / coverage logic
and emits the canonical ``ModelExperimentResult`` from omnibase_core
(OMN-13613) -- it does NOT invent its own result schema.

Acceptance criteria per ticket:
- Contract + entry-point + golden-chain present
- Emits the shared core result contract (ModelExperimentResult)
- All 4 archetype constraints: contract-declared, handler-based, stateless,
  deterministic; no I/O in the handler; topics contract-sourced; no Plugin* base
- Deterministic: same input -> identical ModelExperimentResult

Test strategy: fixture/replay mode only. All per-track metrics (cost, coverage,
token counts, failure classes) are caller-supplied, so the handler is fully
offline and deterministic (no executor, no subprocess, no network).
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from uuid import UUID

import pytest
import yaml
from omnibase_core.enums.enum_experiment_status import EnumExperimentStatus
from omnibase_core.enums.enum_experiment_type import EnumExperimentType
from omnibase_core.models.experiment.model_experiment_result import (
    ModelExperimentResult,
)

from omnimarket.nodes.node_entropy_experiment_orchestrator.handlers.handler_entropy_experiment_orchestrator import (
    HandlerEntropyExperimentOrchestrator,
)
from omnimarket.nodes.node_entropy_experiment_orchestrator.models.model_entropy_experiment_request import (
    ModelEntropyExperimentRequest,
    ModelEntropyTrackInput,
)
from omnimarket.nodes.node_entropy_experiment_orchestrator.models.model_entropy_failure import (
    EntropyFailureClass,
    ModelEntropyFailure,
    entropy_failure_from_exception,
    entropy_failure_from_semantic,
    sanitize_failure_message,
)

# ---------------------------------------------------------------------------
# Shared fixtures (replay-proven: all metrics caller-supplied)
# ---------------------------------------------------------------------------

_EXPERIMENT_ID = UUID("11111111-1111-1111-1111-111111111111")
_RUN_ID = UUID("22222222-2222-2222-2222-222222222222")
_CORRELATION_ID = UUID("33333333-3333-3333-3333-333333333333")
_EVIDENCE_ID = UUID("44444444-4444-4444-4444-444444444444")
_RUNTIME_IDENTITY = "stability-test/runtime-local"

_TRACKS = (
    ModelEntropyTrackInput(
        track_id="omninode:0",
        framework="omninode",
        succeeded=True,
        total_cost_usd=Decimal("0.0021"),
        latency_ms=1450,
        lines_of_code=88,
        test_coverage_pct=92.0,
    ),
    ModelEntropyTrackInput(
        track_id="langchain:0",
        framework="langchain",
        succeeded=True,
        total_cost_usd=Decimal("0.0034"),
        latency_ms=2100,
        lines_of_code=120,
        test_coverage_pct=80.0,
    ),
    ModelEntropyTrackInput(
        track_id="plain_python:0",
        framework="plain_python",
        succeeded=True,
        total_cost_usd=Decimal("0.0009"),
        latency_ms=900,
        lines_of_code=60,
        test_coverage_pct=100.0,
    ),
)

_REQUEST = ModelEntropyExperimentRequest(
    experiment_id=_EXPERIMENT_ID,
    run_id=_RUN_ID,
    correlation_id=_CORRELATION_ID,
    runtime_identity=_RUNTIME_IDENTITY,
    evidence_id=_EVIDENCE_ID,
    tracks=_TRACKS,
)


@pytest.fixture
def node_dir() -> Path:
    return (
        Path(__file__).resolve().parent.parent.parent.parent
        / "src"
        / "omnimarket"
        / "nodes"
        / "node_entropy_experiment_orchestrator"
    )


@pytest.fixture
def contract_path(node_dir: Path) -> Path:
    return node_dir / "contract.yaml"


@pytest.fixture
def metadata_path(node_dir: Path) -> Path:
    return node_dir / "metadata.yaml"


@pytest.fixture
def handler() -> HandlerEntropyExperimentOrchestrator:
    return HandlerEntropyExperimentOrchestrator()


# ---------------------------------------------------------------------------
# Contract / metadata gate (contract-declared archetype constraint)
# ---------------------------------------------------------------------------


class TestContractYaml:
    def test_contract_exists(self, contract_path: Path) -> None:
        assert contract_path.exists()

    def test_contract_loads(self, contract_path: Path) -> None:
        data = yaml.safe_load(contract_path.read_text())
        assert isinstance(data, dict)
        assert data["name"] == "node_entropy_experiment_orchestrator"
        assert data["node_type"] == "orchestrator"
        assert data.get("node_not_implemented") is False

    def test_contract_declares_handler(self, contract_path: Path) -> None:
        data = yaml.safe_load(contract_path.read_text())
        handler_cfg = data.get("handler", {})
        assert "module" in handler_cfg
        assert "class" in handler_cfg

    def test_contract_declares_handler_routing(self, contract_path: Path) -> None:
        data = yaml.safe_load(contract_path.read_text())
        routing = data.get("handler_routing", {})
        handlers = routing.get("handlers", [])
        assert handlers
        assert handlers[0]["handler"]["name"] == "HandlerEntropyExperimentOrchestrator"

    def test_contract_archetype_orchestrator(self, contract_path: Path) -> None:
        data = yaml.safe_load(contract_path.read_text())
        descriptor = data.get("descriptor", {})
        assert descriptor.get("node_archetype") == "orchestrator"

    def test_contract_declares_terminal_event(self, contract_path: Path) -> None:
        data = yaml.safe_load(contract_path.read_text())
        assert "terminal_event" in data
        assert "entropy-experiment-completed" in data["terminal_event"]

    def test_contract_topics_sourced(self, contract_path: Path) -> None:
        """Topics contract-sourced archetype constraint."""
        data = yaml.safe_load(contract_path.read_text())
        bus = data.get("event_bus", {})
        assert bus.get("subscribe_topics")
        assert bus.get("publish_topics")


class TestMetadataYaml:
    def test_metadata_exists(self, metadata_path: Path) -> None:
        assert metadata_path.exists()

    def test_metadata_loads(self, metadata_path: Path) -> None:
        data = yaml.safe_load(metadata_path.read_text())
        assert data["name"] == "node_entropy_experiment_orchestrator"
        assert "version" in data
        assert "entry_points" in data


# ---------------------------------------------------------------------------
# Handler import gate (handler-based archetype constraint)
# ---------------------------------------------------------------------------


class TestHandlerImport:
    def test_handler_module_imports(self) -> None:
        from omnimarket.nodes.node_entropy_experiment_orchestrator.handlers import (
            handler_entropy_experiment_orchestrator,
        )

        assert handler_entropy_experiment_orchestrator is not None

    def test_handler_class_exists(self) -> None:
        assert HandlerEntropyExperimentOrchestrator is not None

    def test_input_model_exists(self) -> None:
        assert ModelEntropyExperimentRequest is not None

    def test_no_plugin_base(self) -> None:
        """No Plugin* base classes archetype constraint."""
        bases = [b.__name__ for b in HandlerEntropyExperimentOrchestrator.__mro__]
        assert not any(name.startswith("Plugin") for name in bases)


# ---------------------------------------------------------------------------
# Core acceptance: emits canonical ModelExperimentResult (the shared contract)
# ---------------------------------------------------------------------------


class TestCanonicalResultEmission:
    def test_returns_core_model_experiment_result(
        self, handler: HandlerEntropyExperimentOrchestrator
    ) -> None:
        result = handler.handle(_REQUEST)
        assert isinstance(result, ModelExperimentResult)

    def test_experiment_type_is_entropy(
        self, handler: HandlerEntropyExperimentOrchestrator
    ) -> None:
        result = handler.handle(_REQUEST)
        assert result.experiment_type == EnumExperimentType.ENTROPY

    def test_identifiers_propagated(
        self, handler: HandlerEntropyExperimentOrchestrator
    ) -> None:
        result = handler.handle(_REQUEST)
        assert result.experiment_id == _EXPERIMENT_ID
        assert result.run_id == _RUN_ID
        assert result.correlation_id == _CORRELATION_ID

    def test_runtime_identity_propagated(
        self, handler: HandlerEntropyExperimentOrchestrator
    ) -> None:
        result = handler.handle(_REQUEST)
        assert result.runtime_identity == _RUNTIME_IDENTITY

    def test_status_completed_when_all_tracks_succeed(
        self, handler: HandlerEntropyExperimentOrchestrator
    ) -> None:
        result = handler.handle(_REQUEST)
        assert result.status == EnumExperimentStatus.COMPLETED

    def test_cost_is_sum_of_track_costs(
        self, handler: HandlerEntropyExperimentOrchestrator
    ) -> None:
        result = handler.handle(_REQUEST)
        expected = sum((t.total_cost_usd for t in _TRACKS), Decimal("0"))
        assert result.cost.cost_usd == expected

    def test_score_is_mean_success_weighted_coverage(
        self, handler: HandlerEntropyExperimentOrchestrator
    ) -> None:
        """Score = fraction of tracks that succeeded (in [0, 1])."""
        result = handler.handle(_REQUEST)
        # all 3 succeeded -> 1.0
        assert result.score.value == pytest.approx(1.0)
        assert result.score.scale_max == pytest.approx(1.0)

    def test_evidence_ref_propagated(
        self, handler: HandlerEntropyExperimentOrchestrator
    ) -> None:
        result = handler.handle(_REQUEST)
        assert result.evidence_ref.evidence_id == _EVIDENCE_ID


# ---------------------------------------------------------------------------
# Determinism (deterministic archetype constraint)
# ---------------------------------------------------------------------------


class TestDeterminism:
    def test_identical_input_identical_output(
        self, handler: HandlerEntropyExperimentOrchestrator
    ) -> None:
        r1 = handler.handle(_REQUEST)
        r2 = handler.handle(_REQUEST)
        assert r1 == r2

    def test_stateless_fresh_handler_same_output(self) -> None:
        r1 = HandlerEntropyExperimentOrchestrator().handle(_REQUEST)
        r2 = HandlerEntropyExperimentOrchestrator().handle(_REQUEST)
        assert r1 == r2


# ---------------------------------------------------------------------------
# Partial / failure aggregation
# ---------------------------------------------------------------------------


class TestFailureAggregation:
    def test_status_failed_when_all_tracks_fail(
        self, handler: HandlerEntropyExperimentOrchestrator
    ) -> None:
        failed_tracks = tuple(
            ModelEntropyTrackInput(
                track_id=t.track_id,
                framework=t.framework,
                succeeded=False,
                total_cost_usd=t.total_cost_usd,
                latency_ms=t.latency_ms,
                lines_of_code=t.lines_of_code,
                test_coverage_pct=None,
                failure_classes=(EntropyFailureClass.MODEL_UNAVAILABLE,),
            )
            for t in _TRACKS
        )
        req = ModelEntropyExperimentRequest(
            experiment_id=_EXPERIMENT_ID,
            run_id=_RUN_ID,
            correlation_id=_CORRELATION_ID,
            runtime_identity=_RUNTIME_IDENTITY,
            evidence_id=_EVIDENCE_ID,
            tracks=failed_tracks,
        )
        result = handler.handle(req)
        assert result.status == EnumExperimentStatus.FAILED
        assert result.score.value == pytest.approx(0.0)

    def test_status_completed_when_partial_success(
        self, handler: HandlerEntropyExperimentOrchestrator
    ) -> None:
        mixed = (
            _TRACKS[0],
            ModelEntropyTrackInput(
                track_id="langchain:0",
                framework="langchain",
                succeeded=False,
                total_cost_usd=Decimal("0.0034"),
                latency_ms=2100,
                lines_of_code=120,
                test_coverage_pct=None,
                failure_classes=(EntropyFailureClass.TIMEOUT,),
            ),
        )
        req = ModelEntropyExperimentRequest(
            experiment_id=_EXPERIMENT_ID,
            run_id=_RUN_ID,
            correlation_id=_CORRELATION_ID,
            runtime_identity=_RUNTIME_IDENTITY,
            evidence_id=_EVIDENCE_ID,
            tracks=mixed,
        )
        result = handler.handle(req)
        assert result.status == EnumExperimentStatus.COMPLETED
        assert result.score.value == pytest.approx(0.5)

    def test_empty_tracks_rejected(
        self, handler: HandlerEntropyExperimentOrchestrator
    ) -> None:
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            ModelEntropyExperimentRequest(
                experiment_id=_EXPERIMENT_ID,
                run_id=_RUN_ID,
                correlation_id=_CORRELATION_ID,
                runtime_identity=_RUNTIME_IDENTITY,
                evidence_id=_EVIDENCE_ID,
                tracks=(),
            )


# ---------------------------------------------------------------------------
# Absorbed SEA failure taxonomy (parity)
# ---------------------------------------------------------------------------


class TestAbsorbedFailureTaxonomy:
    def test_failure_classes_present(self) -> None:
        assert EntropyFailureClass.MODEL_UNAVAILABLE
        assert EntropyFailureClass.TIMEOUT
        assert EntropyFailureClass.INVALID_RESPONSE
        assert EntropyFailureClass.EXTRACTION_FAILED
        assert EntropyFailureClass.COVERAGE_FAILED
        assert EntropyFailureClass.CONTRACT_INVALID
        assert EntropyFailureClass.TOKEN_USAGE_MISSING
        assert EntropyFailureClass.UNKNOWN

    def test_from_exception_timeout(self) -> None:
        assert (
            entropy_failure_from_exception(TimeoutError("timed out"))
            == EntropyFailureClass.TIMEOUT
        )

    def test_from_exception_connection(self) -> None:
        assert (
            entropy_failure_from_exception(ConnectionError("unavailable"))
            == EntropyFailureClass.MODEL_UNAVAILABLE
        )

    def test_from_exception_value_error(self) -> None:
        assert (
            entropy_failure_from_exception(ValueError("bad"))
            == EntropyFailureClass.INVALID_RESPONSE
        )

    def test_from_exception_unknown(self) -> None:
        assert (
            entropy_failure_from_exception(RuntimeError("?"))
            == EntropyFailureClass.UNKNOWN
        )

    def test_from_semantic_none(self) -> None:
        assert entropy_failure_from_semantic(None) == EntropyFailureClass.UNKNOWN

    def test_from_semantic_unknown_string(self) -> None:
        assert (
            entropy_failure_from_semantic("not_a_real_class")
            == EntropyFailureClass.UNKNOWN
        )

    def test_sanitize_strips_local_paths(self) -> None:
        local_prefix = "/" + "Users" + "/jonah"
        msg = f"error at {local_prefix}/secret/file.py line 3"
        out = sanitize_failure_message(msg)
        assert "/" + "Users" + "/" not in out
        assert "<local_path>" in out

    def test_sanitize_bounds_length(self) -> None:
        out = sanitize_failure_message("x" * 2000)
        assert len(out) <= 500

    def test_failure_record_frozen(self) -> None:
        from pydantic import ValidationError

        fail = ModelEntropyFailure(failure_class=EntropyFailureClass.TIMEOUT)
        with pytest.raises((ValidationError, TypeError)):
            fail.framework = "x"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Absorbed SEA coverage parsing (parity, pure)
# ---------------------------------------------------------------------------


class TestAbsorbedCoverage:
    def test_parse_coverage_json(self, tmp_path: Path) -> None:
        from omnimarket.nodes.node_entropy_experiment_orchestrator.models.model_coverage_metrics import (
            parse_coverage_json,
        )

        p = tmp_path / "coverage.json"
        p.write_text('{"totals": {"percent_covered": 87.5}}')
        assert parse_coverage_json(p) == pytest.approx(87.5)

    def test_parse_coverage_json_missing_file(self, tmp_path: Path) -> None:
        from omnimarket.nodes.node_entropy_experiment_orchestrator.models.model_coverage_metrics import (
            CoverageJsonError,
            parse_coverage_json,
        )

        with pytest.raises(CoverageJsonError):
            parse_coverage_json(tmp_path / "missing.json")

    def test_coverage_metrics_frozen(self) -> None:
        from pydantic import ValidationError

        from omnimarket.nodes.node_entropy_experiment_orchestrator.models.model_coverage_metrics import (
            ModelCoverageMetrics,
        )

        m = ModelCoverageMetrics(status="not_run", tests_discovered=0)
        with pytest.raises((ValidationError, TypeError)):
            m.tests_discovered = 5  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Model schema validation (strongly typed archetype constraint)
# ---------------------------------------------------------------------------


class TestModelSchemas:
    def test_request_is_frozen(self) -> None:
        from pydantic import ValidationError

        with pytest.raises((ValidationError, TypeError)):
            _REQUEST.runtime_identity = "mutated"  # type: ignore[misc]

    def test_track_is_frozen(self) -> None:
        from pydantic import ValidationError

        with pytest.raises((ValidationError, TypeError)):
            _TRACKS[0].framework = "mutated"  # type: ignore[misc]

    def test_request_uses_uuid_identifiers(self) -> None:
        assert isinstance(_REQUEST.experiment_id, UUID)
        assert isinstance(_REQUEST.run_id, UUID)
        assert isinstance(_REQUEST.correlation_id, UUID)
        assert isinstance(_REQUEST.evidence_id, UUID)
