# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The frozen tracking record of the weekly coding test (OMN-24177, plan task T13).

The record freezes what every tracking run shares: the window epoch, the seed the sample was
drawn with, the sample size per band, the sampled tasks and their digest, the generation
settings and the prompt template. Changing any of it is a new record, so its sha256 is part of
every run id and a chart never mixes two records. The record is data a deployment supplies in
the node's overlay; the package holds none.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from omnimarket.models.code_eval_trial.model_code_eval_run_request import (
    ModelCodeEvalCandidate,
)
from omnimarket.models.eval_tracking.enum_eval_tracking_cadence import (
    EnumEvalTrackingCadence,
)


def sample_digest(candidates: tuple[ModelCodeEvalCandidate, ...]) -> str:
    """The sha256 of the sampled tasks, order-free: one line per task, sorted."""
    lines = sorted(
        f"{c.repository}|{c.merge_commit}|{c.contract_id}" for c in candidates
    )
    return hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()


class ModelEvalTrackingArm(BaseModel):
    """One tracked model, pinned to its backend, and the cadence it runs on."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    model: str = Field(min_length=1)
    backend_id: str = Field(min_length=1)
    cadence: EnumEvalTrackingCadence


class ModelEvalTrackingRecord(BaseModel):
    """``evals.tracking.record``: frozen once the first window opens."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    epoch: dt.datetime = Field(
        description="Window 0 opens here (UTC); every window is counted from it, never "
        "from a deployment or a last run."
    )
    seed: str = Field(min_length=1, description="The seed the sample was drawn with.")
    band_target: int = Field(ge=1, description="Sampled tasks per usable size band.")
    tenant_id: str = Field(min_length=1)
    harness_revision: str = Field(min_length=1)
    generation_settings: dict[str, str] = Field(
        description="The generation settings every run uses, as written."
    )
    prompt_template: str = Field(min_length=1)
    candidates: tuple[ModelCodeEvalCandidate, ...] = Field(min_length=1)
    task_sample_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    arms: tuple[ModelEvalTrackingArm, ...] = Field(min_length=1)

    @field_validator("epoch")
    @classmethod
    def _epoch_is_utc(cls, value: dt.datetime) -> dt.datetime:
        if value.tzinfo is None or value.utcoffset() != dt.timedelta(0):
            raise ValueError("epoch must be a UTC timestamp")
        return value

    @model_validator(mode="after")
    def _record_is_consistent(self) -> ModelEvalTrackingRecord:
        if sample_digest(self.candidates) != self.task_sample_digest:
            raise ValueError("task_sample_digest does not match the candidates")
        if len({a.model for a in self.arms}) != len(self.arms):
            raise ValueError("a model appears in two arms")
        if len(set(self.candidates)) != len(self.candidates):
            raise ValueError("a task appears twice in the sample")
        return self

    def sha256(self) -> str:
        """The record's identity: the sha256 of its canonical JSON."""
        canonical = json.dumps(
            self.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


__all__ = ["ModelEvalTrackingArm", "ModelEvalTrackingRecord", "sample_digest"]
