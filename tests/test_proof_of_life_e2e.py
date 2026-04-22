"""Proof of Life — end-to-end verification of the unified LLM review workflow.

Capstone test for OMN-7781. Exercises the complete hostile_reviewer pipeline:
  1. Workflow runner drives FSM through all phases
  2. Orchestrator fans out to multiple models (stubbed)
  3. Responses are parsed into ModelReviewFinding instances
  4. Findings are aggregated with dedup and severity promotion
  5. Convergence metrics are computed per-model
  6. Training data records are constructed from pipeline artifacts
  7. Contract YAML loads and declares all handlers
  8. All package exports resolve without import cycles

Reference: OMN-7807, OMN-7781
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
import yaml

from omnimarket.nodes.node_finding_aggregator_compute.handlers.handler_finding_aggregator import (
    HandlerFindingAggregator,
)
from omnimarket.nodes.node_finding_aggregator_compute.models.model_finding_aggregator_config import (
    ModelFindingAggregatorConfig,
)
from omnimarket.nodes.node_finding_aggregator_compute.models.model_finding_aggregator_input import (
    ModelFindingAggregatorInput,
    ModelSourceFindings,
)
from omnimarket.nodes.node_finding_aggregator_compute.models.model_finding_aggregator_output import (
    EnumAggregatedVerdict,
)
from omnimarket.nodes.node_hostile_reviewer.handlers.adapter_inference_bridge import (
    ModelInferenceAdapter,
)
from omnimarket.nodes.node_hostile_reviewer.handlers.handler_convergence_reducer import (
    ModelConvergenceInput,
    ModelFindingLabel,
    compute_convergence,
)
from omnimarket.nodes.node_hostile_reviewer.handlers.handler_prompt_builder import (
    ModelPromptBuilderInput,
    build_prompt,
)
from omnimarket.nodes.node_hostile_reviewer.handlers.handler_response_parser import (
    EnumParseStatus,
    parse_model_response,
)
from omnimarket.nodes.node_hostile_reviewer.handlers.handler_workflow_runner import (
    ModelWorkflowInput,
    ModelWorkflowOutput,
    run_hostile_review_workflow,
)
from omnimarket.nodes.node_hostile_reviewer.models.model_hostile_reviewer_state import (
    EnumHostileReviewerPhase,
)
from omnimarket.nodes.node_hostile_reviewer.models.model_review_finding import (
    EnumFindingCategory,
    EnumFindingSeverity,
    EnumReviewVerdict,
)
from omnimarket.nodes.node_hostile_reviewer.models.model_training_data import (
    EnumLabelSource,
    ModelTrainingDataRecord,
)

# --- Stub responses simulating real model output ---

_RESPONSE_DEEPSEEK = json.dumps(
    [
        {
            "category": "security",
            "severity": "critical",
            "title": "Hardcoded API key in config module",
            "description": "API key is committed as a string literal. Rotate immediately and use env vars.",
            "evidence": 'API_KEY = "REDACTED-EXAMPLE-KEY"',
            "proposed_fix": "Read from os.environ or a secrets manager.",
            "location": "src/config.py",
        },
        {
            "category": "correctness",
            "severity": "major",
            "title": "Race condition in session cache",
            "description": "Concurrent requests can read stale session data; dict access is not thread-safe.",
            "evidence": "session_cache[user_id] = data",
            "proposed_fix": "Use threading.Lock or a thread-safe dict.",
            "location": "src/auth/sessions.py",
        },
    ]
)

_RESPONSE_QWEN = json.dumps(
    [
        {
            "category": "security",
            "severity": "critical",
            "title": "Hardcoded API key in config module",
            "description": "String literal API key in source. Must use environment variable or vault.",
            "evidence": 'API_KEY = "REDACTED-EXAMPLE-KEY"',
            "proposed_fix": "Inject via ONEX_API_KEY env var.",
            "location": "src/config.py",
        },
        {
            "category": "style",
            "severity": "nit",
            "title": "Unused import in auth module",
            "description": "The 'json' import on line 3 is never referenced.",
            "evidence": "import json  # unused",
            "proposed_fix": "Remove the import.",
            "location": "src/auth/__init__.py",
        },
    ]
)

_DIFF_CONTENT = """\
--- a/src/config.py
+++ b/src/config.py
@@ -1,3 +1,5 @@
+API_KEY = "sk-live-abc123"
+SESSION_TTL = 3600
 import os

--- a/src/auth/sessions.py
+++ b/src/auth/sessions.py
@@ -10,3 +10,5 @@
+    session_cache[user_id] = data
+    return session_cache[user_id]
"""


class StubMultiModelAdapter(ModelInferenceAdapter):
    """Deterministic stub returning model-specific canned responses."""

    def __init__(self, responses: dict[str, str]) -> None:
        self._responses = responses
        self.call_log: list[tuple[str, int, int]] = []

    async def infer(
        self,
        model_key: str,
        system_prompt: str,
        user_prompt: str,
        timeout_seconds: float,
    ) -> str:
        self.call_log.append((model_key, len(system_prompt), len(user_prompt)))
        resp = self._responses.get(model_key)
        if resp is None:
            msg = f"No stub for {model_key}"
            raise ValueError(msg)
        return resp


@pytest.mark.unit
class TestProofOfLifeE2E:
    """Capstone proof-of-life: every handler, model, and adapter in the unified pipeline."""

    async def test_workflow_runner_drives_full_pipeline(self) -> None:
        """run_hostile_review_workflow exercises FSM + orchestrator + parser + aggregator."""
        adapter = StubMultiModelAdapter(
            {"deepseek-r1": _RESPONSE_DEEPSEEK, "qwen3-coder": _RESPONSE_QWEN}
        )
        correlation_id = uuid4()

        result = await run_hostile_review_workflow(
            ModelWorkflowInput(
                correlation_id=correlation_id,
                diff_content=_DIFF_CONTENT,
                model_keys=["deepseek-r1", "qwen3-coder"],
                model_context_windows={"deepseek-r1": 64_000, "qwen3-coder": 32_000},
                prompt_template_id="adversarial_reviewer_pr",
            ),
            inference_adapter=adapter,
        )

        assert isinstance(result, ModelWorkflowOutput)
        assert result.correlation_id == correlation_id
        assert result.final_phase == EnumHostileReviewerPhase.DONE
        assert result.error_message is None
        assert result.pass_count >= 1
        assert result.total_findings >= 2

        # Orchestrator output present and populated
        orch = result.orchestrator_output
        assert orch is not None
        assert set(orch.models_succeeded) == {"deepseek-r1", "qwen3-coder"}
        assert len(orch.models_failed) == 0
        assert orch.total_input_findings == 4  # 2 from each model
        assert orch.verdict == EnumReviewVerdict.BLOCKING_ISSUE  # critical finding

        # Dedup: security finding reported by both models -> merged
        assert (
            len(orch.merged_findings) == 3
        )  # security (merged), race condition, unused import
        security = next(f for f in orch.merged_findings if "API key" in f.title)
        assert set(security.source_models) == {"deepseek-r1", "qwen3-coder"}
        assert security.severity == EnumFindingSeverity.CRITICAL

        # Adapter was called for both models
        assert len(adapter.call_log) == 2
        assert {c[0] for c in adapter.call_log} == {"deepseek-r1", "qwen3-coder"}

    async def test_convergence_metrics_from_workflow_output(self) -> None:
        """Convergence reducer produces valid F1 from workflow findings."""
        # Simulate: deepseek-r1 detected 2 findings, frontier detected 3
        labels = [
            ModelFindingLabel(
                finding_id=uuid4(),
                category=EnumFindingCategory.SECURITY,
                local_detected=True,
                frontier_detected=True,
            ),
            ModelFindingLabel(
                finding_id=uuid4(),
                category=EnumFindingCategory.LOGIC_ERROR,
                local_detected=True,
                frontier_detected=True,
            ),
            ModelFindingLabel(
                finding_id=uuid4(),
                category=EnumFindingCategory.STYLE,
                local_detected=False,
                frontier_detected=True,
            ),
        ]
        conv = compute_convergence(
            ModelConvergenceInput(model_key="deepseek-r1", labels=labels)
        )

        assert conv.model_key == "deepseek-r1"
        assert conv.true_positives == 2
        assert conv.false_negatives == 1
        assert conv.false_positives == 0
        assert conv.overall_f1 == pytest.approx(0.8, abs=0.01)
        assert conv.overall_precision == 1.0
        assert conv.overall_recall == pytest.approx(2 / 3, abs=0.01)
        assert "security" in conv.by_category
        assert conv.by_category["security"] == 1.0

    async def test_training_data_record_construction(self) -> None:
        """Training data records round-trip from pipeline artifacts."""
        correlation_id = uuid4()
        session_id = uuid4()
        now = datetime.now(tz=UTC)

        record = ModelTrainingDataRecord(
            id=uuid4(),
            correlation_id=correlation_id,
            session_id=session_id,
            model_key="deepseek-r1",
            category=EnumFindingCategory.SECURITY,
            severity=EnumFindingSeverity.CRITICAL,
            code_diff_hash=hashlib.sha256(_DIFF_CONTENT.encode()).hexdigest(),
            prompt_hash=hashlib.sha256(b"system+user prompt").hexdigest(),
            model_response_hash=hashlib.sha256(_RESPONSE_DEEPSEEK.encode()).hexdigest(),
            local_detected=True,
            frontier_detected=True,
            label_source=EnumLabelSource.FRONTIER_BOOTSTRAP,
            recorded_at=now,
        )

        # Round-trip through JSON
        record_json = record.model_dump(mode="json")
        restored = ModelTrainingDataRecord(**record_json)

        assert restored.correlation_id == correlation_id
        assert restored.model_key == "deepseek-r1"
        assert restored.category == EnumFindingCategory.SECURITY
        assert restored.severity == EnumFindingSeverity.CRITICAL
        assert restored.local_detected is True
        assert restored.frontier_detected is True
        assert restored.label_source == EnumLabelSource.FRONTIER_BOOTSTRAP
        assert (
            restored.code_diff_hash
            == hashlib.sha256(_DIFF_CONTENT.encode()).hexdigest()
        )

    async def test_finding_aggregator_produces_verdict(self) -> None:
        """Standalone finding aggregator produces correct verdict from parsed findings."""
        parse_ds = parse_model_response(_RESPONSE_DEEPSEEK, source_model="deepseek-r1")
        parse_qw = parse_model_response(_RESPONSE_QWEN, source_model="qwen3-coder")

        assert parse_ds.status == EnumParseStatus.SUCCESS
        assert parse_qw.status == EnumParseStatus.SUCCESS

        # Build aggregator input using title as normalized_message for dedup
        # (the aggregator uses Jaccard on normalized_message tokens for matching)
        def _to_agg_finding(f: object) -> dict[str, object]:
            return {
                "rule_id": f.title[:40],  # type: ignore[union-attr]
                "file_path": f.evidence.file_path or "unknown",  # type: ignore[union-attr]
                "line_start": 1,
                "severity": "error"
                if f.severity  # type: ignore[union-attr]
                in (EnumFindingSeverity.CRITICAL, EnumFindingSeverity.MAJOR)
                else "warning",
                "normalized_message": f"{f.title} {f.description}",  # type: ignore[union-attr]
            }

        aggregator = HandlerFindingAggregator()
        agg_output = await aggregator.handle(
            uuid4(),
            ModelFindingAggregatorInput(
                correlation_id=uuid4(),
                sources=(
                    ModelSourceFindings(
                        model_name="deepseek-r1",
                        findings=tuple(_to_agg_finding(f) for f in parse_ds.findings),
                    ),
                    ModelSourceFindings(
                        model_name="qwen3-coder",
                        findings=tuple(_to_agg_finding(f) for f in parse_qw.findings),
                    ),
                ),
                config=ModelFindingAggregatorConfig(
                    jaccard_threshold=0.3,
                    model_weights={"deepseek-r1": 0.6, "qwen3-coder": 0.4},
                ),
            ),
        )

        assert agg_output.source_model_count == 2
        assert agg_output.total_input_findings == 4
        assert agg_output.verdict in (
            EnumAggregatedVerdict.BLOCKING_ISSUE,
            EnumAggregatedVerdict.RISKS_NOTED,
        )
        # At least one finding should be multi-model (security dedup)
        multi = [f for f in agg_output.merged_findings if len(f.source_models) > 1]
        assert len(multi) >= 1

    async def test_contract_yaml_declares_all_handlers(self) -> None:
        """Contract YAML routes the workflow entry point and exposes the inference adapter.

        Per OMN-9269 the contract uses the canonical multi-handler orchestrator
        pattern: a single `handler_routing.handlers[]` entry wiring the
        externally-addressable workflow runner to the start command. Internal
        helpers (prompt builder, response parser, convergence reducer, FSM,
        finding aggregator, review orchestrator) are invoked synchronously by
        the workflow runner and are NOT independent bus subscribers.
        """
        contract_path = (
            Path(__file__).parent.parent
            / "src"
            / "omnimarket"
            / "nodes"
            / "node_hostile_reviewer"
            / "contract.yaml"
        )
        with open(contract_path) as f:
            data = yaml.safe_load(f)

        assert data["contract_version"]["major"] >= 3
        assert data["node_type"] == "workflow"

        handlers = data["handler_routing"]["handlers"]
        assert len(handlers) == 1, (
            "workflow exposes exactly one externally-addressable handler entry"
        )
        entry = handlers[0]
        assert entry["handler"]["name"] == "HandlerWorkflowRunner"
        assert entry["event_model"]["name"] == "ModelHostileReviewerStartCommand"

        adapters = {a["name"] for a in data["handler_routing"]["adapters"]}
        assert "AdapterInferenceBridge" in adapters

        # Event bus topics
        assert len(data["event_bus"]["publish_topics"]) >= 4
        assert len(data["event_bus"]["subscribe_topics"]) == len(handlers)

    async def test_all_package_exports_resolve(self) -> None:
        """All __init__.py exports resolve without import cycles."""
        # Node-level exports
        # Finding aggregator node
        from omnimarket.nodes.node_finding_aggregator_compute.handlers.handler_finding_aggregator import (  # noqa: F401
            HandlerFindingAggregator,
        )
        from omnimarket.nodes.node_hostile_reviewer import (  # noqa: F401
            EnumHostileReviewerPhase,
            HandlerHostileReviewer,
            ModelHostileReviewerCompletedEvent,
            ModelHostileReviewerPhaseEvent,
            ModelHostileReviewerStartCommand,
            ModelHostileReviewerState,
        )

        # Handler-level exports
        from omnimarket.nodes.node_hostile_reviewer.handlers import (  # noqa: F401
            AdapterInferenceBridge,
            ModelConvergenceInput,
            ModelConvergenceOutput,
            ModelFindingLabel,
            ModelInferenceAdapter,
            ModelInferenceBridgeConfig,
            ModelMergedFinding,
            ModelOrchestratorInput,
            ModelOrchestratorOutput,
            ModelParseResult,
            ModelPerModelResult,
            ModelPromptBuilderInput,
            ModelPromptBuilderOutput,
            ModelWorkflowInput,
            ModelWorkflowOutput,
            build_prompt,
            compute_convergence,
            parse_model_response,
            run_hostile_review_workflow,
            run_review_orchestration,
        )

        # Model-level exports
        from omnimarket.nodes.node_hostile_reviewer.models.model_training_data import (  # noqa: F401
            EnumLabelSource,
            ModelTrainingDataRecord,
        )

    async def test_prompt_builder_both_templates(self) -> None:
        """Prompt builder handles both PR and plan templates."""
        for template_id in ("adversarial_reviewer_pr", "adversarial_reviewer_plan"):
            output = build_prompt(
                ModelPromptBuilderInput(
                    prompt_template_id=template_id,
                    context_content=_DIFF_CONTENT,
                    model_context_window=32_000,
                )
            )
            assert len(output.system_prompt) > 100
            assert len(output.user_prompt) > len(_DIFF_CONTENT)
            assert output.truncated is False
