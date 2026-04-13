"""Golden chain integration test for the unified LLM review workflow.

End-to-end: submit review request -> orchestrator dispatches to models via
inference adapter (stubbed) -> responses parsed -> findings aggregated ->
convergence reducer updated -> training data stored -> FSM reaches DONE.

Asserts field-by-field, not just count > 0.

Reference: OMN-7806, OMN-7781
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest

from omnibase_core.event_bus.event_bus_inmemory import EventBusInmemory
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
    ModelFindingAggregatorOutput,
)
from omnimarket.nodes.node_hostile_reviewer.handlers.adapter_inference_bridge import (
    ModelInferenceAdapter,
)
from omnimarket.nodes.node_hostile_reviewer.handlers.handler_convergence_reducer import (
    ModelConvergenceInput,
    ModelConvergenceOutput,
    ModelFindingLabel,
    compute_convergence,
)
from omnimarket.nodes.node_hostile_reviewer.handlers.handler_hostile_reviewer import (
    HandlerHostileReviewer,
)
from omnimarket.nodes.node_hostile_reviewer.handlers.handler_prompt_builder import (
    ModelPromptBuilderInput,
    ModelPromptBuilderOutput,
    build_prompt,
)
from omnimarket.nodes.node_hostile_reviewer.handlers.handler_response_parser import (
    EnumParseStatus,
    ModelParseResult,
    parse_model_response,
)
from omnimarket.nodes.node_hostile_reviewer.handlers.handler_review_orchestrator import (
    ModelOrchestratorInput,
    ModelOrchestratorOutput,
    run_review_orchestration,
)
from omnimarket.nodes.node_hostile_reviewer.models.model_hostile_reviewer_start_command import (
    ModelHostileReviewerStartCommand,
)
from omnimarket.nodes.node_hostile_reviewer.models.model_hostile_reviewer_state import (
    EnumHostileReviewerPhase,
)
from omnimarket.nodes.node_hostile_reviewer.models.model_review_finding import (
    EnumFindingCategory,
    EnumFindingSeverity,
    EnumReviewConfidence,
    EnumReviewVerdict,
)

# --- Topics ---
CMD_TOPIC = "onex.cmd.omnimarket.hostile-reviewer-start.v1"
PHASE_TOPIC = "onex.evt.omnimarket.hostile-reviewer-phase-transition.v1"
COMPLETED_TOPIC = "onex.evt.omnimarket.hostile-reviewer-completed.v1"

# --- Stub LLM responses ---
_STUB_FINDING_SECURITY = {
    "category": "security",
    "severity": "critical",
    "title": "SQL injection in user query builder",
    "description": "The query builder concatenates user input directly into SQL without parameterization.",
    "evidence": "query = f\"SELECT * FROM users WHERE name = '{user_input}'\"",
    "proposed_fix": "Use parameterized queries via cursor.execute(sql, params).",
    "location": "src/db/query_builder.py",
}

_STUB_FINDING_LOGIC = {
    "category": "correctness",
    "severity": "major",
    "title": "Off-by-one in pagination boundary",
    "description": "Pagination returns one extra record when offset equals total count.",
    "evidence": "if offset <= total_count:",
    "proposed_fix": "Use strict less-than: if offset < total_count.",
    "location": "src/api/paginator.py",
}

_STUB_FINDING_STYLE = {
    "category": "style",
    "severity": "nit",
    "title": "Inconsistent import ordering",
    "description": "Imports are not sorted per isort convention.",
    "evidence": "import os\\nimport sys\\nimport json",
    "proposed_fix": "Run isort on the file.",
    "location": "src/utils/helpers.py",
}

# Model A returns security + logic findings
_MODEL_A_RESPONSE = json.dumps([_STUB_FINDING_SECURITY, _STUB_FINDING_LOGIC])

# Model B returns security (duplicate) + style finding
_MODEL_B_RESPONSE = json.dumps([_STUB_FINDING_SECURITY, _STUB_FINDING_STYLE])


class StubInferenceAdapter(ModelInferenceAdapter):
    """Returns canned responses keyed by model_key."""

    def __init__(self, responses: dict[str, str]) -> None:
        self._responses = responses
        self.calls: list[dict[str, object]] = []

    async def infer(
        self,
        model_key: str,
        system_prompt: str,
        user_prompt: str,
        timeout_seconds: float,
    ) -> str:
        self.calls.append(
            {
                "model_key": model_key,
                "system_prompt_len": len(system_prompt),
                "user_prompt_len": len(user_prompt),
                "timeout_seconds": timeout_seconds,
            }
        )
        response = self._responses.get(model_key)
        if response is None:
            msg = f"No stub response for model: {model_key}"
            raise ValueError(msg)
        return response


class TrainingDataStore:
    """In-memory stub for training data storage (OMN-7800 placeholder)."""

    def __init__(self) -> None:
        self.records: list[dict[str, object]] = []

    def store(
        self,
        correlation_id: UUID,
        model_key: str,
        prompt_input: ModelPromptBuilderOutput,
        raw_response: str,
        parsed: ModelParseResult,
        convergence: ModelConvergenceOutput | None = None,
    ) -> None:
        self.records.append(
            {
                "correlation_id": str(correlation_id),
                "model_key": model_key,
                "system_prompt_len": len(prompt_input.system_prompt),
                "user_prompt_len": len(prompt_input.user_prompt),
                "truncated": prompt_input.truncated,
                "raw_response_len": len(raw_response),
                "parse_status": parsed.status.value,
                "findings_count": len(parsed.findings),
                "convergence_f1": convergence.overall_f1 if convergence else None,
            }
        )


@pytest.mark.unit
class TestGoldenChainLlmWorkflow:
    """End-to-end golden chain: review request through full pipeline to DONE."""

    async def test_full_chain_field_by_field(self, event_bus: EventBusInmemory) -> None:
        """Full chain: command -> prompts -> inference -> parse -> aggregate ->
        convergence -> training data -> FSM DONE. Assert field-by-field."""
        correlation_id = uuid4()
        diff_content = (
            "--- a/src/db/query_builder.py\n"
            "+++ b/src/db/query_builder.py\n"
            "@@ -10,3 +10,5 @@\n"
            "+    query = f\"SELECT * FROM users WHERE name = '{user_input}'\"\n"
            "+    return cursor.execute(query)\n"
        )

        # --- Step 1: FSM start ---
        fsm_handler = HandlerHostileReviewer()
        command = ModelHostileReviewerStartCommand(
            correlation_id=correlation_id,
            pr_number=42,
            repo="OmniNode-ai/omnimarket",
            models=["model-a", "model-b"],
            dry_run=False,
            requested_at=datetime.now(tz=UTC),
        )
        state = fsm_handler.start(command)
        assert state.current_phase == EnumHostileReviewerPhase.INIT
        assert state.correlation_id == correlation_id
        assert state.dry_run is False
        assert state.consecutive_failures == 0

        # --- Step 2: INIT -> DISPATCH_REVIEWS ---
        state, event_dispatch = fsm_handler.advance(state, phase_success=True)
        assert state.current_phase == EnumHostileReviewerPhase.DISPATCH_REVIEWS
        assert event_dispatch.from_phase == EnumHostileReviewerPhase.INIT
        assert event_dispatch.to_phase == EnumHostileReviewerPhase.DISPATCH_REVIEWS
        assert event_dispatch.success is True

        # --- Step 3: Build prompts per model ---
        prompt_a = build_prompt(
            ModelPromptBuilderInput(
                prompt_template_id="adversarial_reviewer_pr",
                context_content=diff_content,
                model_context_window=32_000,
            )
        )
        assert isinstance(prompt_a.system_prompt, str)
        assert len(prompt_a.system_prompt) > 100
        assert "adversarial" in prompt_a.system_prompt.lower()
        assert diff_content in prompt_a.user_prompt
        assert prompt_a.truncated is False
        assert prompt_a.original_content_chars == len(diff_content)

        prompt_b = build_prompt(
            ModelPromptBuilderInput(
                prompt_template_id="adversarial_reviewer_pr",
                context_content=diff_content,
                model_context_window=8_000,
            )
        )
        assert isinstance(prompt_b.system_prompt, str)
        assert prompt_b.original_content_chars == len(diff_content)

        # --- Step 4: Dispatch inference via orchestrator ---
        adapter = StubInferenceAdapter(
            responses={"model-a": _MODEL_A_RESPONSE, "model-b": _MODEL_B_RESPONSE}
        )
        orch_input = ModelOrchestratorInput(
            correlation_id=correlation_id,
            diff_content=diff_content,
            model_keys=["model-a", "model-b"],
            model_context_windows={"model-a": 32_000, "model-b": 8_000},
            prompt_template_id="adversarial_reviewer_pr",
        )
        orch_output = await run_review_orchestration(orch_input, adapter)

        # Verify orchestrator output field-by-field
        assert isinstance(orch_output, ModelOrchestratorOutput)
        assert orch_output.correlation_id == correlation_id
        assert set(orch_output.models_succeeded) == {"model-a", "model-b"}
        assert len(orch_output.models_failed) == 0
        assert orch_output.total_input_findings == 4  # 2 from A + 2 from B
        assert (
            len(orch_output.merged_findings) == 3
        )  # security deduped, logic + style unique
        assert (
            orch_output.verdict == EnumReviewVerdict.BLOCKING_ISSUE
        )  # critical finding

        # Verify per-model results
        assert len(orch_output.per_model_results) == 2
        model_a_result = next(
            r for r in orch_output.per_model_results if r.model_key == "model-a"
        )
        assert model_a_result.findings_count == 2
        assert model_a_result.parse_status == EnumParseStatus.SUCCESS
        assert model_a_result.error_message == ""

        model_b_result = next(
            r for r in orch_output.per_model_results if r.model_key == "model-b"
        )
        assert model_b_result.findings_count == 2
        assert model_b_result.parse_status == EnumParseStatus.SUCCESS

        # Verify merged findings field-by-field
        security_finding = next(
            f
            for f in orch_output.merged_findings
            if f.title == "SQL injection in user query builder"
        )
        assert security_finding.severity == EnumFindingSeverity.CRITICAL
        assert set(security_finding.source_models) == {"model-a", "model-b"}

        logic_finding = next(
            f
            for f in orch_output.merged_findings
            if f.title == "Off-by-one in pagination boundary"
        )
        assert logic_finding.severity == EnumFindingSeverity.MAJOR
        assert logic_finding.source_models == ("model-a",)

        style_finding = next(
            f
            for f in orch_output.merged_findings
            if f.title == "Inconsistent import ordering"
        )
        assert style_finding.severity == EnumFindingSeverity.NIT
        assert style_finding.source_models == ("model-b",)

        # Verify adapter was called correctly
        assert len(adapter.calls) == 2
        call_keys = {c["model_key"] for c in adapter.calls}
        assert call_keys == {"model-a", "model-b"}

        # --- Step 5: DISPATCH_REVIEWS -> AGGREGATE ---
        state, event_agg = fsm_handler.advance(
            state, phase_success=True, findings=orch_output.total_input_findings
        )
        assert state.current_phase == EnumHostileReviewerPhase.AGGREGATE
        assert state.total_findings == 4
        assert event_agg.from_phase == EnumHostileReviewerPhase.DISPATCH_REVIEWS
        assert event_agg.to_phase == EnumHostileReviewerPhase.AGGREGATE

        # --- Step 6: Parse responses individually (verify parser) ---
        parse_a = parse_model_response(_MODEL_A_RESPONSE, source_model="model-a")
        assert parse_a.status == EnumParseStatus.SUCCESS
        assert len(parse_a.findings) == 2
        assert parse_a.raw_length == len(_MODEL_A_RESPONSE)

        finding_security = next(
            f for f in parse_a.findings if f.severity == EnumFindingSeverity.CRITICAL
        )
        assert finding_security.category == EnumFindingCategory.SECURITY
        assert finding_security.source_model == "model-a"
        assert finding_security.title == "SQL injection in user query builder"
        assert "parameterization" in finding_security.description
        assert finding_security.evidence.file_path == "src/db/query_builder.py"
        assert finding_security.evidence.code_snippet is not None
        assert finding_security.confidence == EnumReviewConfidence.MEDIUM  # default

        finding_logic = next(
            f for f in parse_a.findings if f.severity == EnumFindingSeverity.MAJOR
        )
        assert finding_logic.category == EnumFindingCategory.LOGIC_ERROR
        assert finding_logic.source_model == "model-a"
        assert finding_logic.title == "Off-by-one in pagination boundary"

        parse_b = parse_model_response(_MODEL_B_RESPONSE, source_model="model-b")
        assert parse_b.status == EnumParseStatus.SUCCESS
        assert len(parse_b.findings) == 2

        # --- Step 7: Run finding aggregator (standalone) ---
        aggregator = HandlerFindingAggregator()
        agg_input = ModelFindingAggregatorInput(
            correlation_id=correlation_id,
            sources=(
                ModelSourceFindings(
                    model_name="model-a",
                    findings=tuple(
                        {
                            "rule_id": f.title[:40],
                            "file_path": f.evidence.file_path or "unknown",
                            "line_start": 10,
                            "severity": "error"
                            if f.severity
                            in (
                                EnumFindingSeverity.CRITICAL,
                                EnumFindingSeverity.MAJOR,
                            )
                            else "warning",
                            "normalized_message": f.description,
                        }
                        for f in parse_a.findings
                    ),
                ),
                ModelSourceFindings(
                    model_name="model-b",
                    findings=tuple(
                        {
                            "rule_id": f.title[:40],
                            "file_path": f.evidence.file_path or "unknown",
                            "line_start": 10,
                            "severity": "error"
                            if f.severity
                            in (
                                EnumFindingSeverity.CRITICAL,
                                EnumFindingSeverity.MAJOR,
                            )
                            else "warning",
                            "normalized_message": f.description,
                        }
                        for f in parse_b.findings
                    ),
                ),
            ),
            config=ModelFindingAggregatorConfig(
                jaccard_threshold=0.7,
                model_weights={"model-a": 0.6, "model-b": 0.4},
            ),
        )
        agg_output = await aggregator.handle(correlation_id, agg_input)

        assert isinstance(agg_output, ModelFindingAggregatorOutput)
        assert agg_output.correlation_id == correlation_id
        assert agg_output.source_model_count == 2
        assert agg_output.total_input_findings == 4
        assert agg_output.total_merged_findings >= 2  # security deduped across models
        assert agg_output.verdict in (
            EnumAggregatedVerdict.BLOCKING_ISSUE,
            EnumAggregatedVerdict.RISKS_NOTED,
        )

        # Verify at least one merged finding has multiple source models (dedup worked)
        multi_source = [
            f for f in agg_output.merged_findings if len(f.source_models) > 1
        ]
        assert len(multi_source) >= 1
        deduped_finding = multi_source[0]
        assert deduped_finding.merged_count >= 2
        assert deduped_finding.weighted_score > 0.0
        assert deduped_finding.weighted_score <= 1.0

        # --- Step 8: AGGREGATE -> CONVERGENCE_CHECK ---
        state, event_conv = fsm_handler.advance(state, phase_success=True)
        assert state.current_phase == EnumHostileReviewerPhase.CONVERGENCE_CHECK
        assert event_conv.success is True

        # --- Step 9: Run convergence reducer ---
        conv_input = ModelConvergenceInput(
            model_key="model-a",
            labels=[
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
            ],
        )
        conv_output = compute_convergence(conv_input)

        assert isinstance(conv_output, ModelConvergenceOutput)
        assert conv_output.model_key == "model-a"
        assert conv_output.true_positives == 2
        assert conv_output.false_positives == 0
        assert conv_output.false_negatives == 1
        assert conv_output.total_labels == 3
        assert conv_output.overall_precision == 1.0
        assert conv_output.overall_recall == pytest.approx(2 / 3, abs=0.01)
        assert conv_output.overall_f1 == pytest.approx(0.8, abs=0.01)
        assert "security" in conv_output.by_category
        assert conv_output.by_category["security"] == 1.0
        assert "logic_error" in conv_output.by_category

        # --- Step 10: Store training data ---
        training_store = TrainingDataStore()
        training_store.store(
            correlation_id=correlation_id,
            model_key="model-a",
            prompt_input=prompt_a,
            raw_response=_MODEL_A_RESPONSE,
            parsed=parse_a,
            convergence=conv_output,
        )
        training_store.store(
            correlation_id=correlation_id,
            model_key="model-b",
            prompt_input=prompt_b,
            raw_response=_MODEL_B_RESPONSE,
            parsed=parse_b,
            convergence=None,
        )

        assert len(training_store.records) == 2
        record_a = next(
            r for r in training_store.records if r["model_key"] == "model-a"
        )
        assert record_a["correlation_id"] == str(correlation_id)
        assert record_a["parse_status"] == "success"
        assert record_a["findings_count"] == 2
        assert record_a["convergence_f1"] == pytest.approx(0.8, abs=0.01)
        assert record_a["system_prompt_len"] > 0
        assert record_a["user_prompt_len"] > 0
        assert record_a["raw_response_len"] == len(_MODEL_A_RESPONSE)
        assert record_a["truncated"] is False

        record_b = next(
            r for r in training_store.records if r["model_key"] == "model-b"
        )
        assert record_b["findings_count"] == 2
        assert record_b["convergence_f1"] is None

        # --- Step 11: CONVERGENCE_CHECK -> REPORT ---
        state, event_report = fsm_handler.advance(state, phase_success=True)
        assert state.current_phase == EnumHostileReviewerPhase.REPORT
        assert event_report.success is True

        # --- Step 12: REPORT -> DONE ---
        state, event_done = fsm_handler.advance(state, phase_success=True)
        assert state.current_phase == EnumHostileReviewerPhase.DONE
        assert event_done.from_phase == EnumHostileReviewerPhase.REPORT
        assert event_done.to_phase == EnumHostileReviewerPhase.DONE
        assert event_done.success is True
        assert state.error_message is None
        assert state.consecutive_failures == 0

        # Verify completed event
        completed = fsm_handler.make_completed_event(
            state, started_at=command.requested_at
        )
        assert completed.correlation_id == correlation_id
        assert completed.final_phase == EnumHostileReviewerPhase.DONE
        assert completed.error_message is None
        assert completed.started_at == command.requested_at
        assert completed.completed_at > completed.started_at

    async def test_full_chain_event_bus_wiring(
        self, event_bus: EventBusInmemory
    ) -> None:
        """Verify the full chain wires through EventBusInmemory correctly."""
        correlation_id = uuid4()
        completed_events: list[dict[str, object]] = []
        phase_events: list[dict[str, object]] = []

        async def on_command(message: object) -> None:
            payload = json.loads(message.value)  # type: ignore[union-attr]
            command = ModelHostileReviewerStartCommand(
                correlation_id=payload["correlation_id"],
                pr_number=payload.get("pr_number"),
                repo=payload.get("repo"),
                models=payload.get("models", ["model-a", "model-b"]),
                dry_run=payload.get("dry_run", False),
                requested_at=datetime.now(tz=UTC),
            )

            handler = HandlerHostileReviewer()
            _state, events, completed = handler.run_full_pipeline(command)

            for evt in events:
                phase_payload = evt.model_dump(mode="json")
                phase_events.append(phase_payload)
                await event_bus.publish(
                    PHASE_TOPIC,
                    key=None,
                    value=json.dumps(phase_payload).encode(),
                )

            completed_payload = completed.model_dump(mode="json")
            completed_events.append(completed_payload)
            await event_bus.publish(
                COMPLETED_TOPIC,
                key=None,
                value=json.dumps(completed_payload).encode(),
            )

        await event_bus.start()
        await event_bus.subscribe(
            CMD_TOPIC, on_message=on_command, group_id="test-golden-chain-llm"
        )

        cmd_payload = json.dumps(
            {
                "correlation_id": str(correlation_id),
                "pr_number": 42,
                "repo": "OmniNode-ai/omnimarket",
                "models": ["model-a", "model-b"],
            }
        ).encode()
        await event_bus.publish(CMD_TOPIC, key=None, value=cmd_payload)

        # Verify FSM completed through event bus
        assert len(completed_events) == 1
        assert completed_events[0]["final_phase"] == "done"
        assert completed_events[0]["correlation_id"] == str(correlation_id)
        assert completed_events[0]["error_message"] is None

        # 5 phase transitions: INIT->DISPATCH, DISPATCH->AGGREGATE,
        # AGGREGATE->CONVERGENCE, CONVERGENCE->REPORT, REPORT->DONE
        assert len(phase_events) == 5
        assert phase_events[0]["from_phase"] == "init"
        assert phase_events[0]["to_phase"] == "dispatch_reviews"
        assert phase_events[-1]["from_phase"] == "report"
        assert phase_events[-1]["to_phase"] == "done"
        assert all(e["success"] is True for e in phase_events)

        await event_bus.close()

    async def test_chain_with_inference_failure_triggers_circuit_breaker(
        self, event_bus: EventBusInmemory
    ) -> None:
        """When inference fails, FSM circuit breaker activates after 3 failures."""
        handler = HandlerHostileReviewer()
        command = ModelHostileReviewerStartCommand(
            correlation_id=uuid4(),
            pr_number=99,
            repo="OmniNode-ai/omnimarket",
            dry_run=False,
            requested_at=datetime.now(tz=UTC),
        )
        state = handler.start(command)

        # INIT -> DISPATCH_REVIEWS (success)
        state, _ = handler.advance(state, phase_success=True)
        assert state.current_phase == EnumHostileReviewerPhase.DISPATCH_REVIEWS

        # Simulate 3 inference failures
        state, evt1 = handler.advance(
            state, phase_success=False, error_message="model-a: connection timeout"
        )
        assert state.consecutive_failures == 1
        assert state.current_phase == EnumHostileReviewerPhase.DISPATCH_REVIEWS
        assert evt1.success is False
        assert evt1.error_message == "model-a: connection timeout"

        state, _evt2 = handler.advance(
            state, phase_success=False, error_message="model-b: rate limited"
        )
        assert state.consecutive_failures == 2

        state, evt3 = handler.advance(
            state, phase_success=False, error_message="model-a: connection refused"
        )
        assert state.current_phase == EnumHostileReviewerPhase.FAILED
        assert state.consecutive_failures == 3
        assert evt3.success is False

        completed = handler.make_completed_event(state, started_at=command.requested_at)
        assert completed.final_phase == EnumHostileReviewerPhase.FAILED
        assert completed.error_message is not None

    async def test_chain_with_parse_failure_still_produces_verdict(
        self, event_bus: EventBusInmemory
    ) -> None:
        """When one model returns unparseable output, the chain still works with the other."""
        adapter = StubInferenceAdapter(
            responses={
                "model-a": _MODEL_A_RESPONSE,
                "model-b": "This is not valid JSON at all, just plain text.",
            }
        )
        orch_input = ModelOrchestratorInput(
            correlation_id=uuid4(),
            diff_content="some diff content",
            model_keys=["model-a", "model-b"],
            model_context_windows={"model-a": 32_000, "model-b": 32_000},
        )
        result = await run_review_orchestration(orch_input, adapter)

        assert "model-a" in result.models_succeeded
        assert "model-b" in result.models_failed
        assert result.total_input_findings == 2  # only from model-a
        assert len(result.merged_findings) == 2
        assert result.verdict == EnumReviewVerdict.BLOCKING_ISSUE

        # Verify model-b parse failure recorded
        model_b_result = next(
            r for r in result.per_model_results if r.model_key == "model-b"
        )
        assert model_b_result.parse_status == EnumParseStatus.FORMAT_FAILURE
        assert model_b_result.findings_count == 0

    async def test_convergence_reducer_per_category_with_mixed_results(
        self, event_bus: EventBusInmemory
    ) -> None:
        """Convergence reducer tracks per-category F1 with mixed detection results."""
        labels = [
            # Security: both detected (TP)
            ModelFindingLabel(
                finding_id=uuid4(),
                category=EnumFindingCategory.SECURITY,
                local_detected=True,
                frontier_detected=True,
            ),
            # Security: local missed (FN)
            ModelFindingLabel(
                finding_id=uuid4(),
                category=EnumFindingCategory.SECURITY,
                local_detected=False,
                frontier_detected=True,
            ),
            # Logic: local-only (FP)
            ModelFindingLabel(
                finding_id=uuid4(),
                category=EnumFindingCategory.LOGIC_ERROR,
                local_detected=True,
                frontier_detected=False,
            ),
            # Integration: both detected (TP)
            ModelFindingLabel(
                finding_id=uuid4(),
                category=EnumFindingCategory.INTEGRATION,
                local_detected=True,
                frontier_detected=True,
            ),
        ]
        result = compute_convergence(
            ModelConvergenceInput(model_key="model-a", labels=labels)
        )

        assert result.true_positives == 2
        assert result.false_positives == 1
        assert result.false_negatives == 1
        assert result.total_labels == 4

        # Security: 1 TP, 0 FP, 1 FN -> F1 = 2*1/(2+1) = 0.667
        assert result.by_category["security"] == pytest.approx(2 / 3, abs=0.01)
        # Logic: 0 TP, 1 FP, 0 FN -> F1 = 0.0
        assert result.by_category["logic_error"] == 0.0
        # Integration: 1 TP, 0 FP, 0 FN -> F1 = 1.0
        assert result.by_category["integration"] == 1.0

    async def test_prompt_builder_truncation_for_large_content(
        self, event_bus: EventBusInmemory
    ) -> None:
        """Prompt builder truncates content exceeding model context window."""
        large_content = "x" * 100_000
        result = build_prompt(
            ModelPromptBuilderInput(
                prompt_template_id="adversarial_reviewer_pr",
                context_content=large_content,
                model_context_window=4_096,
            )
        )

        assert result.truncated is True
        assert result.original_content_chars == 100_000
        assert result.truncated_content_chars < 100_000
        assert "truncated" in result.user_prompt.lower()
        assert len(result.system_prompt) > 0

    async def test_finding_aggregator_dedup_and_severity_promotion(
        self, event_bus: EventBusInmemory
    ) -> None:
        """Aggregator deduplicates findings and promotes severity on conflict."""
        aggregator = HandlerFindingAggregator()
        agg_input = ModelFindingAggregatorInput(
            correlation_id=uuid4(),
            sources=(
                ModelSourceFindings(
                    model_name="model-a",
                    findings=(
                        {
                            "rule_id": "SQL-001",
                            "file_path": "src/db.py",
                            "line_start": 10,
                            "severity": "warning",
                            "normalized_message": "SQL injection vulnerability in query builder",
                        },
                    ),
                ),
                ModelSourceFindings(
                    model_name="model-b",
                    findings=(
                        {
                            "rule_id": "SQL-001",
                            "file_path": "src/db.py",
                            "line_start": 10,
                            "severity": "error",
                            "normalized_message": "SQL injection vulnerability in query builder function",
                        },
                    ),
                ),
            ),
            config=ModelFindingAggregatorConfig(
                jaccard_threshold=0.5,
                severity_promotes_on_conflict=True,
            ),
        )
        result = await aggregator.handle(uuid4(), agg_input)

        assert result.total_input_findings == 2
        assert result.total_merged_findings == 1
        assert result.total_duplicates_removed == 1

        merged = result.merged_findings[0]
        assert merged.rule_id == "SQL-001"
        assert merged.file_path == "src/db.py"
        assert merged.severity == "error"  # promoted from warning
        assert set(merged.source_models) == {"model-a", "model-b"}
        assert merged.merged_count == 2
        assert merged.weighted_score > 0.0
