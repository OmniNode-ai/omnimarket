# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Content-addressed measurement wire models, shared with promotion consumers.

These are evidence types, not a promotion validator. Promotion policy lives in core.
"""

from typing import Annotated

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, JsonValue

Digest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


class ModelMeasurementFact(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class ModelMeasurementOverlay(ModelMeasurementFact):
    sample_limit: int = Field(gt=0)
    freshness_days: int = Field(gt=0)
    restatement_window_days: int = Field(gt=0)
    schedule_seconds: int = Field(gt=0)
    wilson_z: float = Field(gt=0)
    labeller_engine: str = Field(min_length=1)
    write_identity: str = Field(min_length=1)
    allowed_write_logins: tuple[str, ...] = Field(min_length=1)
    shadow_mode: str
    hint_mode: str
    dispatch_mode: str
    confirmation_actors: tuple[str, ...] = Field(min_length=1)


class ModelMeasurementCommand(ModelMeasurementFact):
    now: AwareDatetime
    row_digest: Digest
    corpus_digest: Digest
    normalizer_digest: Digest
    live_digest: Digest


class ModelRuleMeasurementSnapshot(ModelMeasurementFact):
    id: str = Field(min_length=1)
    mode: str
    author_engine: str = Field(min_length=1)
    definition: dict[str, JsonValue]
    live_since: AwareDatetime | None = None
    hint_since: AwareDatetime | None = None


class ModelReplayRecord(ModelMeasurementFact):
    record_id: str = Field(min_length=1)
    lane: str = Field(min_length=1)
    occurred_at: AwareDatetime
    event: dict[str, JsonValue]


class ModelLiveMatch(ModelMeasurementFact):
    match_id: str = Field(min_length=1)
    row_id: str
    row_digest: Digest
    mode: str
    occurred_at: AwareDatetime


class ModelOperatorCapture(ModelMeasurementFact):
    capture_id: str = Field(min_length=1)
    occurred_at: AwareDatetime
    text: str


class ModelRestatementConfirmation(ModelMeasurementFact):
    capture_id: str
    match_id: str
    actor: str = Field(min_length=1)
    source_ref: str = Field(min_length=1)
    confirmed_at: AwareDatetime


class ModelLiveMeasurementInput(ModelMeasurementFact):
    matches: tuple[ModelLiveMatch, ...]
    captures: tuple[ModelOperatorCapture, ...]
    confirmations: tuple[ModelRestatementConfirmation, ...] = ()


class ModelMatchLabel(ModelMeasurementFact):
    record_id: str
    applies: bool


class ModelRestatementLabel(ModelMeasurementFact):
    capture_id: str
    match_id: str | None
    applies: bool


class ModelLabellingRequest(ModelMeasurementFact):
    row_digest: Digest
    corpus_digest: Digest
    normalizer_digest: Digest
    row: ModelRuleMeasurementSnapshot
    sample: tuple[ModelReplayRecord, ...]
    captures: tuple[ModelOperatorCapture, ...]
    matches: tuple[ModelLiveMatch, ...]
    engine: str


class ModelLabelSet(ModelMeasurementFact):
    engine: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    row_digest: Digest
    corpus_digest: Digest
    normalizer_digest: Digest
    matches: tuple[ModelMatchLabel, ...]
    restatements: tuple[ModelRestatementLabel, ...] = ()


class ModelWilsonInterval(ModelMeasurementFact):
    lower: float = Field(ge=0, le=1)
    upper: float = Field(ge=0, le=1)


class ModelProspectiveMissRate(ModelMeasurementFact):
    hint_since: AwareDatetime
    observed_until: AwareDatetime
    live_matches: int = Field(gt=0)
    restatements: int = Field(ge=0)
    miss_rate: float = Field(ge=0)
    restatement_ids: tuple[str, ...]
    pending_confirmation_ids: tuple[str, ...]


class ModelMeasuredErrorRule(ModelMeasurementFact):
    row_digest: Digest
    row_snapshot_digest: Digest
    corpus_digest: Digest
    labels_digest: Digest
    normalizer_digest: Digest
    live_digest: Digest
    labeller_engine: str
    labeller_run_id: str
    measured_at: AwareDatetime
    written_by: str
    hits: int = Field(ge=0)
    labelled_matches: int = Field(ge=0)
    sample_weight: float = Field(ge=0)
    sample_ids: tuple[str, ...]
    false_matches: int = Field(ge=0)
    true_matches: int = Field(ge=0)
    true_lanes: tuple[str, ...]
    true_days: tuple[str, ...]
    oldest_true_match: AwareDatetime | None
    false_match_rate: float | None = Field(default=None, ge=0, le=1)
    false_match_wilson: ModelWilsonInterval | None = None
    disagreements: tuple[str, ...]
    prospective: ModelProspectiveMissRate | None = None


class ModelMeasurementProposal(ModelMeasurementFact):
    row_id: str
    idempotency_key: str
    write_identity: str
    measured: ModelMeasuredErrorRule


class ModelCommittedMeasurement(ModelMeasurementFact):
    commit_id: str
    login: str
    row_id: str
    measured: ModelMeasuredErrorRule


class ModelDemotionProposal(ModelMeasurementFact):
    row_id: str
    row_snapshot_digest: Digest
    idempotency_key: str
    target_mode: str
    capture_ids: tuple[str, ...]
    measured: ModelMeasuredErrorRule


class ModelMeasurementResult(ModelMeasurementFact):
    measured: ModelMeasuredErrorRule
    commit_id: str
    read_back: bool
    demotion_pr: str | None = None


class ModelMeasurementTick(ModelMeasurementFact):
    now: AwareDatetime


class ModelDailyMeasurementSnapshot(ModelMeasurementFact):
    row_digests: tuple[Digest, ...]
    corpus_digest: Digest
    normalizer_digest: Digest
    live_digest: Digest


class ModelDailyMeasurementResult(ModelMeasurementFact):
    measured_at: AwareDatetime
    measurements: tuple[ModelMeasurementResult, ...]
