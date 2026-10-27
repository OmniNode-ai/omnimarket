# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""This offline compute node takes a frozen JSONL transcript, its session identifier
and audited must-survive labels. Each label names a source record and required
text spans. The handler accepts a retention detector and parsed overlay through
constructor injection; the caller owns overlay loading and transcript reads.
The supplied overlay disables live runtime selection. It permits only the
policies implemented by this version.

The result binds the exact transcript and every boundary prefix with SHA-256.
It records transcript wall time, final API usage per message id, separate cache
usage and boundary context sizes and duration. Input tokens exclude cache reads
and cache creation; those counters are reported separately. Missing timestamps
make wall time unavailable. Missing API usage makes session totals unavailable,
while observed subtotals and missing message ids remain visible. Boundary sizes
are context occupancy, not summarizer API usage.

Only explicitly marked compact summaries are scored. A missing summary is
unavailable, never a retained or dropped result. Every labelled fact or decision
introduced before a boundary is checked against that boundary's summary. Labels
remain obligations at later boundaries; callers must supply labels that remain
in force for the transcript being measured. A missing required span is lexical
loss. Paraphrases, obedience, contradictions and unlabelled obligations require
separate evaluation; this node makes no semantic or experiment-success claim.
It performs no model calls and assigns no experimental arm.

Run the synthetic controls with:

```bash
uv run --frozen pytest tests/nodes/node_session_compaction_measurement_compute -q --no-cov
```
"""

from __future__ import annotations

from hashlib import sha256
from typing import Protocol

from ..models.model_measurement import (
    ModelBoundaryMeasurement,
    ModelMeasurementOverlay,
    ModelMeasurementRequest,
    ModelMeasurementResult,
    ModelTranscriptRecord,
    ModelUsage,
)


class ProtocolRetentionDetector(Protocol):
    def missing(self, spans: tuple[str, ...], summary: str) -> bool: ...


class LexicalRetentionDetector:
    def missing(self, spans: tuple[str, ...], summary: str) -> bool:
        return any(span.casefold() not in summary.casefold() for span in spans)


def _digest(text: str) -> str:
    return sha256(text.encode("utf-8")).hexdigest()


class HandlerSessionCompactionMeasurement:
    """Measure one frozen transcript; inject the retention comparison policy."""

    def __init__(
        self,
        *,
        detector: ProtocolRetentionDetector | None = None,
        overlay: ModelMeasurementOverlay | None = None,
    ) -> None:
        self._detector = (
            detector if detector is not None else LexicalRetentionDetector()
        )

        self.overlay = overlay if overlay is not None else ModelMeasurementOverlay()

    def handle(self, request: ModelMeasurementRequest) -> ModelMeasurementResult:
        lines = request.transcript_jsonl.splitlines(keepends=True)
        records = tuple(
            ModelTranscriptRecord.model_validate_json(line) for line in lines
        )
        sources: dict[str, int] = {}
        usage: dict[str, ModelUsage | None] = {}
        boundaries: list[int] = []
        for index, record in enumerate(records):
            if (
                record.session_id is not None
                and record.session_id != request.session_id
            ):
                raise ValueError(f"Mixed session at record {index}")
            if record.uuid:
                if record.uuid in sources:
                    raise ValueError(f"Duplicate record uuid: {record.uuid}")
                sources[record.uuid] = index
            if record.type == "system" and record.subtype == "compact_boundary":
                if not record.uuid:
                    raise ValueError("A compaction boundary requires a uuid")
                boundaries.append(index)
            if record.type == "assistant":
                message = record.message
                key = (message.id if message else None) or record.uuid
                if not key:
                    raise ValueError(
                        "An assistant usage record requires a message id or uuid"
                    )
                usage[key] = message.usage if message else None
        # Label evidence must be in a conversation text record, never a tool
        # result or thinking block. Later labels cannot leak into earlier cases.
        evidence_detector = LexicalRetentionDetector()
        for item in request.expected_items:
            source = sources.get(item.source_record_uuid)
            if source is None:
                raise ValueError(f"Missing label source: {item.source_record_uuid}")
            if evidence_detector.missing(
                item.required_spans, records[source].spoken_text()
            ):
                raise ValueError(f"Label spans absent from source: {item.key}")
        results: list[ModelBoundaryMeasurement] = []
        for position, index in enumerate(boundaries):
            boundary = records[index]
            end = (
                boundaries[position + 1]
                if position + 1 < len(boundaries)
                else len(records)
            )
            summaries = [
                record
                for record in records[index + 1 : end]
                if record.is_summary and record.type == "user" and not record.is_meta
            ]
            if len(summaries) > 1:
                raise ValueError(
                    f"Multiple marked summaries for boundary {boundary.uuid}"
                )
            summary = summaries[0].spoken_text() if summaries else None
            retained: list[str] = []
            decisions: list[str] = []
            facts: list[str] = []
            if summary is not None:
                for item in request.expected_items:
                    if sources[item.source_record_uuid] >= index:
                        continue
                    if self._detector.missing(item.required_spans, summary):
                        (decisions if item.kind == "decision" else facts).append(
                            item.key
                        )
                    else:
                        retained.append(item.key)
            results.append(
                ModelBoundaryMeasurement(
                    boundary_uuid=boundary.uuid or "",
                    trigger=boundary.boundary.trigger,
                    timestamp=boundary.timestamp,
                    prefix_sha256=_digest("".join(lines[: index + 1])),
                    summary_sha256=_digest(summary) if summary is not None else None,
                    pre_context_tokens=boundary.boundary.pre_tokens,
                    post_context_tokens=boundary.boundary.post_tokens,
                    duration_ms=boundary.boundary.duration_ms,
                    status="scored" if summary is not None else "summary_unavailable",
                    retained=tuple(retained),
                    dropped_decisions=tuple(decisions),
                    dropped_facts=tuple(facts),
                )
            )
        timestamps = [
            record.timestamp for record in records if record.timestamp is not None
        ]
        complete_time = len(timestamps) == len(records) and bool(timestamps)
        wall_time = (
            int((max(timestamps) - min(timestamps)).total_seconds() * 1000)
            if complete_time
            else None
        )
        known = [value for value in usage.values() if value is not None]
        missing = tuple(key for key, value in usage.items() if value is None)
        complete_usage = bool(usage) and not missing
        observed_in = sum(value.input_tokens for value in known)
        observed_out = sum(value.output_tokens for value in known)
        return ModelMeasurementResult(
            session_id=request.session_id,
            transcript_sha256=_digest(request.transcript_jsonl),
            wall_time_ms=wall_time,
            usage_status="measured" if complete_usage else "unavailable",
            input_tokens=observed_in if complete_usage else None,
            output_tokens=observed_out if complete_usage else None,
            cache_read_input_tokens=sum(
                value.cache_read_input_tokens for value in known
            )
            if complete_usage
            else None,
            cache_creation_input_tokens=sum(
                value.cache_creation_input_tokens for value in known
            )
            if complete_usage
            else None,
            observed_input_tokens=observed_in,
            observed_output_tokens=observed_out,
            missing_usage_messages=missing,
            boundaries=tuple(results),
        )
