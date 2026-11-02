# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerExtractionRungHarness: one schema and one preamble strip for every rung (OMN-21140, TE.3).

Pure, ``handle(ModelRungHarnessRequest) -> ModelRungHarnessResult``. It replays each
recorded call from its recorded bytes alone (no network, no client), strips a reasoning
preamble the same way for every rung, recovers the proposals into the one proposed-event
schema, and counts parse failures apart from misses. It derives a rung's chunk size from
the rung's measured window and records which measurement produced it.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import re

from pydantic import ValidationError

from omnimarket.models.extraction_rung import (
    EnumRungCallOutcome,
    ModelChunkDerivation,
    ModelChunkDerivationParams,
    ModelRecordedRungCall,
    ModelRungCallVerdict,
    ModelRungHarnessRequest,
    ModelRungHarnessResult,
    ModelRungWindowMeasurement,
)
from omnimarket.models.transcript_extraction import ModelProposedExtractionEvent

ANSWER_MARKER = "### ANSWER"
_THINK_BLOCK = re.compile(r"<think>.*?</think>", re.DOTALL)
_FENCE = re.compile(r"```[A-Za-z0-9_-]*\s*\n(.*?)```", re.DOTALL)


def record_call(
    *,
    call_id: str,
    prompt: str,
    response: bytes,
    served_model_id: str,
    expected_quotes: tuple[str, ...] = (),
) -> ModelRecordedRungCall:
    """Pin a call's exact response bytes and its request, ready to replay offline."""
    return ModelRecordedRungCall(
        call_id=call_id,
        request_digest=hashlib.sha256(prompt.encode()).hexdigest(),
        response_b64=base64.b64encode(response).decode("ascii"),
        response_digest=hashlib.sha256(response).hexdigest(),
        served_model_id=served_model_id,
        expected_quotes=expected_quotes,
    )


def _strip_preamble(text: str) -> str | None:
    """The one strip every rung gets; None when the answer cannot be recovered."""
    text = _THINK_BLOCK.sub("", text)
    if "<think>" in text:
        return None
    if "</think>" in text:
        text = text.rsplit("</think>", 1)[1]
    if ANSWER_MARKER in text:
        text = text.rsplit(ANSWER_MARKER, 1)[1]
    for block in _FENCE.findall(text):
        if block.lstrip()[:1] in {"[", "{"}:
            return str(block)
    return text


def _items(text: str) -> list[object] | None:
    """Every candidate event recoverable from text; None when no JSON value decoded, [] for an empty one."""
    decoder = json.JSONDecoder()
    found: list[object] = []
    decoded = False
    index = 0
    while index < len(text):
        if text[index] not in "[{":
            index += 1
            continue
        try:
            value, end = decoder.raw_decode(text, index)
        except json.JSONDecodeError:
            index += 1
            continue
        index = end
        decoded = True
        if isinstance(value, list):
            found.extend(value)
        elif isinstance(value, dict) and isinstance(value.get("events"), list):
            found.extend(value["events"])
        else:
            found.append(value)
    return found if decoded else None


def _normalise(text: str) -> str:
    return " ".join(text.split())


def _derive_chunk(
    window: ModelRungWindowMeasurement | None, params: ModelChunkDerivationParams
) -> ModelChunkDerivation:
    if window is None:
        return ModelChunkDerivation(
            status="unmeasured",
            max_chars=None,
            overlap_chars=params.overlap_chars,
            window_measurement_id=None,
            window_tokens=None,
        )
    usable = (
        window.window_tokens - params.output_reserve_tokens - params.instruction_tokens
    )
    max_chars = int(usable * window.chars_per_token * params.safety_factor)
    too_small = usable <= 0 or max_chars <= params.overlap_chars
    return ModelChunkDerivation(
        status="window_too_small" if too_small else "derived",
        max_chars=None if too_small else max_chars,
        overlap_chars=params.overlap_chars,
        window_measurement_id=window.measurement_id,
        window_tokens=window.window_tokens,
    )


def _replay(call: ModelRecordedRungCall) -> bytes | None:
    try:
        raw = base64.b64decode(call.response_b64, validate=True)
    except (binascii.Error, ValueError):
        return None
    return raw if hashlib.sha256(raw).hexdigest() == call.response_digest else None


def _verdict(call: ModelRecordedRungCall, contracted: str) -> ModelRungCallVerdict:
    attribution_ok = call.served_model_id == contracted
    raw = _replay(call)
    if raw is None:
        return ModelRungCallVerdict(
            call_id=call.call_id,
            outcome=EnumRungCallOutcome.RECORDING_CORRUPT,
            attribution_ok=attribution_ok,
            replayed_b64="",
            replayed_digest="",
            proposals=(),
            parse_failures=0,
            misses=0,
        )
    replayed = {
        "replayed_b64": call.response_b64,
        "replayed_digest": hashlib.sha256(raw).hexdigest(),
    }
    stripped = _strip_preamble(raw.decode("utf-8", errors="replace"))
    items = None if stripped is None else _items(stripped)
    if items is None:
        return ModelRungCallVerdict(
            call_id=call.call_id,
            outcome=EnumRungCallOutcome.PARSE_FAILURE,
            attribution_ok=attribution_ok,
            proposals=(),
            parse_failures=1,
            misses=0,
            **replayed,
        )
    proposals: list[str] = []
    parse_failures = 0
    quotes: set[str] = set()
    for item in items:
        text = json.dumps(item, ensure_ascii=False)
        try:
            event = ModelProposedExtractionEvent.model_validate_json(text)
        except ValidationError:
            parse_failures += 1
            continue
        proposals.append(text)
        quotes.add(_normalise(event.quote))
    # A call that failed to parse is not also a miss: the two counts never overlap.
    misses = (
        0
        if parse_failures
        else sum(1 for q in call.expected_quotes if _normalise(q) not in quotes)
    )
    if parse_failures:
        outcome = EnumRungCallOutcome.PARSE_FAILURE
    elif misses:
        outcome = EnumRungCallOutcome.MISS
    else:
        outcome = EnumRungCallOutcome.HIT
    return ModelRungCallVerdict(
        call_id=call.call_id,
        outcome=outcome,
        attribution_ok=attribution_ok,
        proposals=tuple(proposals),
        parse_failures=parse_failures,
        misses=misses,
        **replayed,
    )


class HandlerExtractionRungHarness:
    """Replay one rung's recorded calls and report hits, misses and parse failures apart."""

    def handle(self, request: ModelRungHarnessRequest) -> ModelRungHarnessResult:
        verdicts = tuple(
            _verdict(c, request.contracted_model_id) for c in request.calls
        )
        return ModelRungHarnessResult(
            rung_id=request.rung_id,
            verdicts=verdicts,
            calls=len(verdicts),
            hits=sum(1 for v in verdicts if v.outcome is EnumRungCallOutcome.HIT),
            parse_failures=sum(v.parse_failures for v in verdicts),
            misses=sum(v.misses for v in verdicts),
            corrupt_recordings=sum(
                1
                for v in verdicts
                if v.outcome is EnumRungCallOutcome.RECORDING_CORRUPT
            ),
            attribution_mismatches=sum(1 for v in verdicts if not v.attribution_ok),
            chunk=_derive_chunk(request.window, request.derivation),
        )


__all__ = ["ANSWER_MARKER", "HandlerExtractionRungHarness", "record_call"]
