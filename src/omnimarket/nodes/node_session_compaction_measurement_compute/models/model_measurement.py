# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed inputs and records for offline transcript measurement.

Labels are caller-owned ground truth, not inferred decisions. Token fields are
API usage counters; boundary context sizes never stand in for summarizer usage.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

Count = Annotated[int, Field(strict=True, ge=0)]
Nonempty = Annotated[str, Field(min_length=1)]


class ModelMustSurviveItem(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    key: Nonempty
    kind: Literal["decision", "fact"]
    source_record_uuid: Nonempty
    required_spans: tuple[Nonempty, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def nonblank(self) -> ModelMustSurviveItem:
        if not self.key.strip() or any(
            not span.strip() for span in self.required_spans
        ):
            raise ValueError("Label keys and required spans must not be blank")
        return self


class ModelMeasurementRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    session_id: Nonempty
    transcript_jsonl: Nonempty
    expected_items: tuple[ModelMustSurviveItem, ...] = ()

    @model_validator(mode="after")
    def unique_items(self) -> ModelMeasurementRequest:
        keys = [item.key for item in self.expected_items]
        if len(keys) != len(set(keys)):
            raise ValueError("Expected item keys must be unique")
        return self


class ModelUsage(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)
    input_tokens: Count
    output_tokens: Count
    cache_read_input_tokens: Count = 0
    cache_creation_input_tokens: Count = 0


class ModelContentBlock(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)
    type: str
    text: str = ""


class ModelMessage(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)
    id: str | None = None
    content: str | tuple[ModelContentBlock, ...] = ""
    usage: ModelUsage | None = None


class ModelBoundaryMetadata(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)
    trigger: str | None = None
    pre_tokens: Count | None = Field(default=None, alias="preTokens")
    post_tokens: Count | None = Field(default=None, alias="postTokens")
    duration_ms: Count | None = Field(default=None, alias="durationMs")


class ModelTranscriptRecord(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)
    type: str
    uuid: str | None = None
    session_id: str | None = Field(default=None, alias="sessionId")
    timestamp: AwareDatetime | None = None
    subtype: str | None = None
    is_summary: bool = Field(default=False, alias="isCompactSummary")
    is_meta: bool = Field(default=False, alias="isMeta")
    message: ModelMessage | None = None
    boundary: ModelBoundaryMetadata = Field(
        default_factory=ModelBoundaryMetadata, alias="compactMetadata"
    )

    def spoken_text(self) -> str:
        if (
            self.type not in ("user", "assistant")
            or self.is_meta
            or self.message is None
        ):
            return ""
        content = self.message.content
        if isinstance(content, str):
            return content
        return "\n".join(block.text for block in content if block.type == "text")


class ModelBoundaryMeasurement(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    boundary_uuid: str
    trigger: str | None
    timestamp: AwareDatetime | None
    prefix_sha256: str
    summary_sha256: str | None
    pre_context_tokens: Count | None
    post_context_tokens: Count | None
    duration_ms: Count | None
    status: Literal["scored", "summary_unavailable"]
    retained: tuple[str, ...] = ()
    dropped_decisions: tuple[str, ...] = ()
    dropped_facts: tuple[str, ...] = ()


class ModelMeasurementResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    session_id: str
    transcript_sha256: str
    wall_time_ms: Count | None
    usage_status: Literal["measured", "unavailable"]
    input_tokens: Count | None
    output_tokens: Count | None
    cache_read_input_tokens: Count | None
    cache_creation_input_tokens: Count | None
    observed_input_tokens: Count
    observed_output_tokens: Count
    missing_usage_messages: tuple[str, ...]
    boundaries: tuple[ModelBoundaryMeasurement, ...]


class ModelMeasurementOverlay(BaseModel):
    """Offline policy supplied as parsed overlay data by the caller."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    node: Literal["node_session_compaction_measurement_compute"] = (
        "node_session_compaction_measurement_compute"
    )
    enabled: Literal[False] = False
    runtime_profiles: tuple[()] = ()
    retention_policy: Literal["lexical_casefold_all_spans"] = (
        "lexical_casefold_all_spans"
    )
    usage_policy: Literal["final_snapshot_per_message_id"] = (
        "final_snapshot_per_message_id"
    )
    summary_policy: Literal["marked_only"] = "marked_only"
