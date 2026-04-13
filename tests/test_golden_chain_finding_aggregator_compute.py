# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden chain test for node_finding_aggregator_compute.

Verifies weighted-union dedup, Jaccard similarity, severity promotion,
model weight computation, and verdict determination.
"""

from __future__ import annotations

import json
from uuid import uuid4

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
)

CMD_TOPIC = "onex.cmd.omnimarket.finding-aggregator-start.v1"
COMPLETED_TOPIC = "onex.evt.omnimarket.finding-aggregator-completed.v1"


def _finding(
    rule_id: str = "R001",
    file_path: str = "src/foo.py",
    line_start: int = 10,
    severity: str = "warning",
    message: str = "unused import detected",
) -> dict[str, object]:
    return {
        "rule_id": rule_id,
        "file_path": file_path,
        "line_start": line_start,
        "line_end": None,
        "severity": severity,
        "normalized_message": message,
    }


@pytest.mark.unit
class TestFindingAggregatorGoldenChain:
    """Golden chain: source findings in -> merged findings out."""

    async def test_single_model_no_dedup(self, event_bus: EventBusInmemory) -> None:
        """Single model with unique findings produces no dedup."""
        handler = HandlerFindingAggregator()
        cid = uuid4()
        input_data = ModelFindingAggregatorInput(
            correlation_id=cid,
            sources=(
                ModelSourceFindings(
                    model_name="deepseek-r1",
                    findings=(
                        _finding(rule_id="R001", message="unused import os"),
                        _finding(
                            rule_id="R002",
                            file_path="src/bar.py",
                            message="type error in func",
                        ),
                    ),
                ),
            ),
        )

        result = await handler.handle(correlation_id=cid, input_data=input_data)

        assert result.correlation_id == cid
        assert result.total_input_findings == 2
        assert result.total_merged_findings == 2
        assert result.total_duplicates_removed == 0
        assert result.source_model_count == 1

    async def test_duplicate_findings_merged(self, event_bus: EventBusInmemory) -> None:
        """Same finding from two models should merge into one."""
        handler = HandlerFindingAggregator()
        cid = uuid4()
        finding = _finding(message="unused import os detected in module")
        input_data = ModelFindingAggregatorInput(
            correlation_id=cid,
            sources=(
                ModelSourceFindings(model_name="deepseek-r1", findings=(finding,)),
                ModelSourceFindings(model_name="qwen3-coder", findings=(finding,)),
            ),
        )

        result = await handler.handle(correlation_id=cid, input_data=input_data)

        assert result.total_input_findings == 2
        assert result.total_merged_findings == 1
        assert result.total_duplicates_removed == 1
        assert len(result.merged_findings[0].source_models) == 2

    async def test_severity_promotion(self, event_bus: EventBusInmemory) -> None:
        """When merging, higher severity should win."""
        handler = HandlerFindingAggregator()
        cid = uuid4()
        input_data = ModelFindingAggregatorInput(
            correlation_id=cid,
            sources=(
                ModelSourceFindings(
                    model_name="model-a",
                    findings=(
                        _finding(
                            severity="warning", message="security issue found in auth"
                        ),
                    ),
                ),
                ModelSourceFindings(
                    model_name="model-b",
                    findings=(
                        _finding(
                            severity="error", message="security issue found in auth"
                        ),
                    ),
                ),
            ),
            config=ModelFindingAggregatorConfig(severity_promotes_on_conflict=True),
        )

        result = await handler.handle(correlation_id=cid, input_data=input_data)

        assert result.total_merged_findings == 1
        assert result.merged_findings[0].severity == "error"

    async def test_no_severity_promotion(self, event_bus: EventBusInmemory) -> None:
        """When severity_promotes_on_conflict=False, first severity wins."""
        handler = HandlerFindingAggregator()
        cid = uuid4()
        input_data = ModelFindingAggregatorInput(
            correlation_id=cid,
            sources=(
                ModelSourceFindings(
                    model_name="model-a",
                    findings=(
                        _finding(
                            severity="warning", message="security issue found in auth"
                        ),
                    ),
                ),
                ModelSourceFindings(
                    model_name="model-b",
                    findings=(
                        _finding(
                            severity="error", message="security issue found in auth"
                        ),
                    ),
                ),
            ),
            config=ModelFindingAggregatorConfig(severity_promotes_on_conflict=False),
        )

        result = await handler.handle(correlation_id=cid, input_data=input_data)

        assert result.total_merged_findings == 1
        assert result.merged_findings[0].severity == "warning"

    async def test_verdict_clean(self, event_bus: EventBusInmemory) -> None:
        """No findings produces CLEAN verdict."""
        handler = HandlerFindingAggregator()
        cid = uuid4()
        input_data = ModelFindingAggregatorInput(
            correlation_id=cid,
            sources=(ModelSourceFindings(model_name="model-a", findings=()),),
        )

        result = await handler.handle(correlation_id=cid, input_data=input_data)

        assert result.verdict == EnumAggregatedVerdict.CLEAN
        assert result.total_merged_findings == 0

    async def test_verdict_blocking_issue(self, event_bus: EventBusInmemory) -> None:
        """Error-severity finding produces BLOCKING_ISSUE verdict."""
        handler = HandlerFindingAggregator()
        cid = uuid4()
        input_data = ModelFindingAggregatorInput(
            correlation_id=cid,
            sources=(
                ModelSourceFindings(
                    model_name="model-a",
                    findings=(_finding(severity="error"),),
                ),
            ),
        )

        result = await handler.handle(correlation_id=cid, input_data=input_data)

        assert result.verdict == EnumAggregatedVerdict.BLOCKING_ISSUE

    async def test_verdict_risks_noted(self, event_bus: EventBusInmemory) -> None:
        """Warning-only findings produce RISKS_NOTED verdict."""
        handler = HandlerFindingAggregator()
        cid = uuid4()
        input_data = ModelFindingAggregatorInput(
            correlation_id=cid,
            sources=(
                ModelSourceFindings(
                    model_name="model-a",
                    findings=(_finding(severity="warning"),),
                ),
            ),
        )

        result = await handler.handle(correlation_id=cid, input_data=input_data)

        assert result.verdict == EnumAggregatedVerdict.RISKS_NOTED

    async def test_missing_fields_skipped(self, event_bus: EventBusInmemory) -> None:
        """Findings with missing required fields are skipped."""
        handler = HandlerFindingAggregator()
        cid = uuid4()
        incomplete: dict[str, object] = {"rule_id": "R001"}  # missing other fields
        input_data = ModelFindingAggregatorInput(
            correlation_id=cid,
            sources=(
                ModelSourceFindings(
                    model_name="model-a",
                    findings=(incomplete, _finding()),
                ),
            ),
        )

        result = await handler.handle(correlation_id=cid, input_data=input_data)

        assert result.total_input_findings == 2
        assert result.total_merged_findings == 1

    async def test_weighted_score_with_custom_weights(
        self, event_bus: EventBusInmemory
    ) -> None:
        """Custom model weights affect weighted_score."""
        handler = HandlerFindingAggregator()
        cid = uuid4()
        finding = _finding(message="test weighted score calculation here")
        input_data = ModelFindingAggregatorInput(
            correlation_id=cid,
            sources=(
                ModelSourceFindings(model_name="heavy", findings=(finding,)),
                ModelSourceFindings(model_name="light", findings=()),
            ),
            config=ModelFindingAggregatorConfig(
                model_weights={"heavy": 0.8, "light": 0.2}
            ),
        )

        result = await handler.handle(correlation_id=cid, input_data=input_data)

        assert result.total_merged_findings == 1
        # heavy model has weight 0.8, so score should reflect that
        assert result.merged_findings[0].weighted_score > 0.5

    async def test_event_bus_wiring(self, event_bus: EventBusInmemory) -> None:
        """Handler events can be wired through EventBusInmemory."""
        handler = HandlerFindingAggregator()
        completed_events: list[dict[str, object]] = []

        async def on_command(message: object) -> None:
            payload = json.loads(message.value)  # type: ignore[union-attr]
            cid = uuid4()
            input_data = ModelFindingAggregatorInput(
                correlation_id=cid,
                sources=(
                    ModelSourceFindings(
                        model_name="test-model",
                        findings=tuple(payload.get("findings", [])),
                    ),
                ),
            )
            result = await handler.handle(correlation_id=cid, input_data=input_data)
            result_dict = result.model_dump(mode="json")
            completed_events.append(result_dict)
            await event_bus.publish(
                COMPLETED_TOPIC,
                key=None,
                value=json.dumps(result_dict).encode(),
            )

        await event_bus.start()
        await event_bus.subscribe(
            CMD_TOPIC,
            on_message=on_command,
            group_id="test-finding-aggregator",
        )

        cmd_payload = json.dumps({"findings": [dict(_finding())]}).encode()
        await event_bus.publish(CMD_TOPIC, key=None, value=cmd_payload)

        assert len(completed_events) == 1
        assert completed_events[0]["verdict"] == "risks_noted"

        await event_bus.close()
