# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The rung harness: one schema, one preamble strip, counted apart, replayed offline (OMN-21140, TE.3)."""

from __future__ import annotations

import base64
import hashlib
import json
import socket
from pathlib import Path
from typing import Any

import pytest
import yaml

from omnimarket.models.extraction_rung import (
    EnumRungCallOutcome,
    ModelChunkDerivationParams,
    ModelRecordedRungCall,
    ModelRungHarnessRequest,
    ModelRungWindowMeasurement,
)
from omnimarket.nodes.node_extraction_rung_harness_compute.handlers.handler_extraction_rung_harness import (
    HandlerExtractionRungHarness,
    record_call,
)

pytestmark = pytest.mark.unit

REPO = Path(__file__).resolve().parents[3]
PARAMS = ModelChunkDerivationParams(
    output_reserve_tokens=1000,
    instruction_tokens=500,
    safety_factor=0.5,
    overlap_chars=100,
)
QUOTE = "Do not deploy without approval."


def _event(quote: str = QUOTE) -> dict[str, Any]:
    return {
        "kind": "ruling",
        "quote": quote,
        "event_id": "e1",
        "prompt_id": "p1",
        "covers_start": 0,
        "covers_end": len(quote),
    }


def _call(
    text: str,
    *,
    served: str = "model-a",
    expected: tuple[str, ...] = (QUOTE,),
    call_id: str = "c1",
) -> ModelRecordedRungCall:
    return record_call(
        call_id=call_id,
        prompt="p",
        response=text.encode(),
        served_model_id=served,
        expected_quotes=expected,
    )


def _run(
    *calls: ModelRecordedRungCall,
    window: ModelRungWindowMeasurement | None = None,
    contracted: str = "model-a",
) -> Any:
    return HandlerExtractionRungHarness().handle(
        ModelRungHarnessRequest(
            rung_id="rung-x",
            contracted_model_id=contracted,
            calls=calls,
            window=window,
            derivation=PARAMS,
        )
    )


SHAPES = {
    "bare_array": json.dumps([_event()]),
    "fenced": "```json\n" + json.dumps([_event()]) + "\n```",
    "think_preamble": "<think>reasoning "
    + "x" * 50
    + "</think>\n"
    + json.dumps([_event()]),
    "answer_marker": "I will reason first.\n### ANSWER\n" + json.dumps([_event()]),
    "prose_preamble": "Here are the events I found:\n" + json.dumps([_event()]),
    "single_object": json.dumps(_event()),
    "ndjson": json.dumps(_event()) + "\n",
    "events_wrapper": json.dumps({"events": [_event()]}),
    "close_only_think": "reasoning</think>" + json.dumps([_event()]),
}


@pytest.mark.parametrize("shape", sorted(SHAPES))
def test_every_recoverable_shape_is_a_hit_and_proposals_share_one_schema(
    shape: str,
) -> None:
    result = _run(_call(SHAPES[shape]))
    verdict = result.verdicts[0]
    assert verdict.outcome is EnumRungCallOutcome.HIT, shape
    assert result.parse_failures == 0
    assert result.misses == 0
    assert [json.loads(p) for p in verdict.proposals] == [_event()]


def test_parse_failures_and_misses_are_counted_apart() -> None:
    parse_bad = _call("no json here at all", call_id="c1")
    invalid_event = _call(json.dumps([{"kind": "ruling"}]), call_id="c2")
    miss = _call(json.dumps([_event("Something else entirely.")]), call_id="c3")
    empty = _call("[]", call_id="c4")
    result = _run(parse_bad, invalid_event, miss, empty)
    outcomes = [v.outcome for v in result.verdicts]
    assert outcomes == [
        EnumRungCallOutcome.PARSE_FAILURE,
        EnumRungCallOutcome.PARSE_FAILURE,
        EnumRungCallOutcome.MISS,
        EnumRungCallOutcome.MISS,
    ]
    assert result.parse_failures == 2
    assert result.misses == 2
    assert result.hits == 0


def test_an_unterminated_think_block_is_a_parse_failure_not_a_recovered_answer() -> (
    None
):
    result = _run(_call("<think>still going " + json.dumps([_event()])))
    assert result.verdicts[0].outcome is EnumRungCallOutcome.PARSE_FAILURE


def test_attribution_mismatch_is_counted() -> None:
    result = _run(_call(SHAPES["bare_array"], served="model-b"))
    assert result.attribution_mismatches == 1
    assert result.verdicts[0].attribution_ok is False
    assert _run(_call(SHAPES["bare_array"])).attribution_mismatches == 0


def test_replay_is_byte_identical_and_opens_no_socket(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw = ("<think>é</think>\n" + json.dumps([_event()])).encode()
    recording = record_call(
        call_id="c1",
        prompt="prompt",
        response=raw,
        served_model_id="model-a",
        expected_quotes=(QUOTE,),
    )

    def _no_network(*_a: object, **_k: object) -> None:
        raise AssertionError("network used during replay")

    monkeypatch.setattr(socket, "socket", _no_network)
    monkeypatch.setattr(socket, "create_connection", _no_network)
    monkeypatch.setattr(socket, "getaddrinfo", _no_network)
    result = _run(recording)
    replayed = base64.b64decode(result.verdicts[0].replayed_b64)
    assert replayed == raw
    assert result.verdicts[0].replayed_digest == hashlib.sha256(raw).hexdigest()
    assert recording.request_digest == hashlib.sha256(b"prompt").hexdigest()


def test_a_tampered_recording_is_corrupt_and_never_replayed_as_a_hit() -> None:
    good = _call(SHAPES["bare_array"])
    tampered = good.model_copy(
        update={"response_b64": base64.b64encode(b"[]").decode()}
    )
    result = _run(tampered)
    assert result.verdicts[0].outcome is EnumRungCallOutcome.RECORDING_CORRUPT
    assert result.corrupt_recordings == 1
    assert result.parse_failures == 0
    assert result.misses == 0


def _window(tokens: int = 9000) -> ModelRungWindowMeasurement:
    return ModelRungWindowMeasurement(
        measurement_id="m1",
        rung_id="rung-x",
        window_tokens=tokens,
        chars_per_token=4.0,
        load_condition="four_slots_busy",
        slots_busy=4,
        measured_at="2026-11-02T00:00:00Z",
    )


def test_chunk_size_is_derived_from_the_measured_window_and_traced() -> None:
    chunk = _run(_call(SHAPES["bare_array"]), window=_window()).chunk
    # (9000 - 1000 - 500) tokens * 4 chars * 0.5 safety
    assert chunk.status == "derived"
    assert chunk.max_chars == 15000
    assert chunk.overlap_chars == 100
    assert chunk.window_measurement_id == "m1"
    assert chunk.window_tokens == 9000


def test_no_measured_window_gives_no_chunk_size() -> None:
    chunk = _run(_call(SHAPES["bare_array"])).chunk
    assert chunk.status == "unmeasured"
    assert chunk.max_chars is None
    assert chunk.window_measurement_id is None


def test_a_window_smaller_than_its_reserves_gives_no_chunk_size() -> None:
    chunk = _run(_call(SHAPES["bare_array"]), window=_window(1400)).chunk
    assert chunk.status == "window_too_small"
    assert chunk.max_chars is None


def test_the_committed_window_config_validates_and_every_window_names_its_measurement() -> (
    None
):
    data = yaml.safe_load(
        (REPO / "src/omnimarket/configs/extraction_rung_windows.v1.yaml").read_text(
            encoding="utf-8"
        )
    )
    ModelChunkDerivationParams.model_validate(data["chunk_derivation"])
    for entry in data["measured_windows"]:
        ModelRungWindowMeasurement.model_validate(entry)


def test_a_real_recorded_delegation_call_replays_byte_identically_offline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = Path(__file__).parent / "fixtures" / "recorded_local_rung_call.json"
    recording = ModelRecordedRungCall.model_validate_json(
        path.read_text(encoding="utf-8")
    )

    def _no_network(*_a: object, **_k: object) -> None:
        raise AssertionError("network used during replay")

    monkeypatch.setattr(socket, "socket", _no_network)
    monkeypatch.setattr(socket, "getaddrinfo", _no_network)
    result = _run(recording, contracted="Qwen3.8-27B")
    verdict = result.verdicts[0]
    assert verdict.outcome is EnumRungCallOutcome.HIT
    assert verdict.attribution_ok
    assert base64.b64decode(verdict.replayed_b64) == base64.b64decode(
        recording.response_b64
    )
    assert verdict.replayed_digest == recording.response_digest
