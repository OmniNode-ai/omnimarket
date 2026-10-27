# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Synthetic controls for deterministic transcript measurement."""

from __future__ import annotations

import importlib
import json
from importlib.metadata import entry_points
from pathlib import Path

import pytest
import yaml
from omnibase_core.nodes.node_compute import NodeCompute
from pydantic import ValidationError

from omnimarket.nodes.node_session_compaction_measurement_compute.handlers.handler_measurement import (
    HandlerSessionCompactionMeasurement,
)
from omnimarket.nodes.node_session_compaction_measurement_compute.models.model_measurement import (
    ModelMeasurementOverlay,
    ModelMeasurementRequest,
    ModelMustSurviveItem,
)
from omnimarket.nodes.node_session_compaction_measurement_compute.node import (
    NodeSessionCompactionMeasurementCompute,
)

pytestmark = pytest.mark.unit
FIXTURE = Path(__file__).parent / "fixtures/session.jsonl"
ROOT = Path(__file__).resolve().parents[3]
NODE = ROOT / "src/omnimarket/nodes/node_session_compaction_measurement_compute"


def request(text: str | None = None) -> ModelMeasurementRequest:
    return ModelMeasurementRequest(
        session_id="session-a",
        transcript_jsonl=FIXTURE.read_text() if text is None else text,
        expected_items=(
            ModelMustSurviveItem(
                key="adapter-choice",
                kind="decision",
                source_record_uuid="u1",
                required_spans=("blue adapter",),
            ),
            ModelMustSurviveItem(
                key="fixture-size",
                kind="fact",
                source_record_uuid="u1",
                required_spans=("seven records",),
            ),
        ),
    )


def records() -> list[dict]:
    return [json.loads(line) for line in FIXTURE.read_text().splitlines()]


def encoded(rows: list[dict]) -> str:
    return "\n".join(json.dumps(row) for row in rows)


def test_dropped_decision_negative_control() -> None:
    result = HandlerSessionCompactionMeasurement().handle(request())
    [boundary] = result.boundaries
    assert boundary.dropped_decisions == ("adapter-choice",)
    assert boundary.dropped_facts == ()
    assert boundary.retained == ("fixture-size",)
    assert boundary.status == "scored"


def test_loss_detector_disabled_mutant_fails_control() -> None:
    class RetainEverything:
        def missing(self, spans: tuple[str, ...], summary: str) -> bool:
            return False

    result = HandlerSessionCompactionMeasurement(detector=RetainEverything()).handle(
        request()
    )
    with pytest.raises(AssertionError):
        assert result.boundaries[0].dropped_decisions == ("adapter-choice",)


def test_wall_time_and_usage_stream_dedup() -> None:
    result = HandlerSessionCompactionMeasurement().handle(request())
    assert result.wall_time_ms == 20000
    assert result.input_tokens == 140
    assert result.output_tokens == 11
    assert result.cache_read_input_tokens == 20
    assert result.cache_creation_input_tokens == 10
    assert result.usage_status == "measured"
    [boundary] = result.boundaries
    assert (
        boundary.boundary_uuid,
        boundary.pre_context_tokens,
        boundary.post_context_tokens,
        boundary.duration_ms,
    ) == ("b1", 1000, 100, 300)
    assert result == HandlerSessionCompactionMeasurement().handle(request())


def test_positive_control_and_dropped_fact() -> None:
    rows = records()
    rows[4]["message"]["content"] = "Use the BLUE adapter. Seven records."
    assert (
        HandlerSessionCompactionMeasurement()
        .handle(request(encoded(rows)))
        .boundaries[0]
        .dropped_decisions
        == ()
    )
    rows[4]["message"]["content"] = "Use the blue adapter."
    assert HandlerSessionCompactionMeasurement().handle(
        request(encoded(rows))
    ).boundaries[0].dropped_facts == ("fixture-size",)


def test_missing_summary_never_uses_next_prompt() -> None:
    rows = records()
    del rows[4]
    boundary = (
        HandlerSessionCompactionMeasurement()
        .handle(request(encoded(rows)))
        .boundaries[0]
    )
    assert boundary.status == "summary_unavailable"
    assert boundary.dropped_decisions == ()
    assert boundary.retained == ()


def test_missing_usage_is_unavailable_not_zero() -> None:
    rows = records()
    del rows[5]["message"]["usage"]
    result = HandlerSessionCompactionMeasurement().handle(request(encoded(rows)))
    assert result.usage_status == "unavailable"
    assert result.input_tokens is None
    assert result.output_tokens is None
    assert result.observed_input_tokens == 100
    assert result.missing_usage_messages == ("m2",)


def test_boundary_sizes_are_not_summarizer_usage() -> None:
    rows = records()
    for row in rows:
        if row["type"] == "assistant":
            del row["message"]["usage"]
    result = HandlerSessionCompactionMeasurement().handle(request(encoded(rows)))
    assert result.input_tokens is None
    assert result.observed_input_tokens == 0
    assert result.boundaries[0].pre_context_tokens == 1000


def test_two_boundaries_and_future_item_not_scored_early() -> None:
    rows = records()
    rows.extend(
        [
            {
                "type": "system",
                "subtype": "compact_boundary",
                "uuid": "b2",
                "timestamp": "2026-01-01T00:00:21Z",
            },
            {
                "type": "user",
                "uuid": "s2",
                "timestamp": "2026-01-01T00:00:22Z",
                "isCompactSummary": True,
                "message": {"content": "Use the blue adapter."},
            },
        ]
    )
    req = request(encoded(rows))
    req = req.model_copy(
        update={
            "expected_items": (
                *req.expected_items,
                ModelMustSurviveItem(
                    key="stop",
                    kind="decision",
                    source_record_uuid="u2",
                    required_spans=("Stop",),
                ),
            )
        }
    )
    first, second = HandlerSessionCompactionMeasurement().handle(req).boundaries
    assert "stop" not in first.dropped_decisions
    assert second.dropped_decisions == ("stop",)
    assert second.dropped_facts == ("fixture-size",)


@pytest.mark.parametrize(
    "mutation",
    [
        "bad_json",
        "mixed_session",
        "duplicate_uuid",
        "missing_source",
        "unbacked_span",
        "negative_usage",
        "missing_boundary_uuid",
        "naive_timestamp",
    ],
)
def test_invalid_inputs_fail_loudly(mutation: str) -> None:
    rows = records()
    if mutation == "bad_json":
        text = "{bad json}"
    else:
        if mutation == "mixed_session":
            rows[1]["sessionId"] = "other"
        elif mutation == "duplicate_uuid":
            rows[1]["uuid"] = "u1"
        elif mutation == "missing_source":
            rows[0]["uuid"] = "other"
        elif mutation == "unbacked_span":
            rows[0]["message"]["content"] = "Unrelated text."
        elif mutation == "negative_usage":
            rows[1]["message"]["usage"]["input_tokens"] = -1
        elif mutation == "missing_boundary_uuid":
            del rows[3]["uuid"]
        elif mutation == "naive_timestamp":
            rows[0]["timestamp"] = "2026-01-01T00:00:00"
        text = encoded(rows)
    with pytest.raises((ValueError, ValidationError)):
        HandlerSessionCompactionMeasurement().handle(request(text))


def test_no_boundaries_and_missing_timestamp() -> None:
    rows = [row for row in records() if row.get("subtype") != "compact_boundary"]
    del rows[-1]["timestamp"]
    result = HandlerSessionCompactionMeasurement().handle(request(encoded(rows)))
    assert result.boundaries == ()
    assert result.wall_time_ms is None


def test_node_contract_and_overlay() -> None:
    contract = yaml.safe_load((NODE / "contract.yaml").read_text())
    overlay = yaml.safe_load((NODE / "overlay.yaml").read_text())
    assert contract["runtime_profiles"] == []
    assert overlay["runtime_profiles"] == []
    assert overlay["enabled"] is False
    assert contract["descriptor"]["purity"] == "pure"
    handler = contract["handler"]
    assert getattr(importlib.import_module(handler["module"]), handler["class"])
    assert issubclass(NodeSessionCompactionMeasurementCompute, NodeCompute)
    entries = entry_points(group="onex.nodes")
    assert any(entry.name == NODE.name for entry in entries)


def test_typed_overlay_injection_and_refusal() -> None:
    overlay = ModelMeasurementOverlay.model_validate(
        yaml.safe_load((NODE / "overlay.yaml").read_text())
    )
    handler = HandlerSessionCompactionMeasurement(overlay=overlay)
    assert handler.overlay == overlay
    assert handler.handle(request()).boundaries[0].dropped_decisions == (
        "adapter-choice",
    )
    for update in (
        {"enabled": True},
        {"summary_policy": "first_user"},
        {"runtime_profiles": ["main"]},
    ):
        with pytest.raises(ValidationError):
            ModelMeasurementOverlay.model_validate({**overlay.model_dump(), **update})


def test_prefix_hash_binds_exact_bytes_and_future_is_excluded() -> None:
    original = HandlerSessionCompactionMeasurement().handle(request())
    appended = (
        request().transcript_jsonl
        + '{"type":"progress","timestamp":"2026-01-01T00:00:30Z"}\n'
    )
    changed = HandlerSessionCompactionMeasurement().handle(request(appended))
    assert original.boundaries[0].prefix_sha256 == changed.boundaries[0].prefix_sha256
    assert original.transcript_sha256 != changed.transcript_sha256
    spaced = request().transcript_jsonl.replace('"u1"', ' "u1"', 1)
    assert (
        original.boundaries[0].prefix_sha256
        != HandlerSessionCompactionMeasurement()
        .handle(request(spaced))
        .boundaries[0]
        .prefix_sha256
    )


def test_unlabelled_text_and_tool_results_do_not_create_ground_truth() -> None:
    rows = records()
    rows[0]["message"]["content"] = [
        {"type": "tool_result", "content": "Use the blue adapter. Seven records."}
    ]
    with pytest.raises(ValueError, match="Label spans absent"):
        HandlerSessionCompactionMeasurement().handle(request(encoded(rows)))
    result = HandlerSessionCompactionMeasurement().handle(
        request().model_copy(update={"expected_items": ()})
    )
    assert result.boundaries[0].dropped_decisions == ()
    assert result.boundaries[0].dropped_facts == ()


def test_multiple_marked_summaries_are_ambiguous() -> None:
    rows = records()
    rows[6]["isCompactSummary"] = True
    with pytest.raises(ValueError, match="Multiple marked summaries"):
        HandlerSessionCompactionMeasurement().handle(request(encoded(rows)))
