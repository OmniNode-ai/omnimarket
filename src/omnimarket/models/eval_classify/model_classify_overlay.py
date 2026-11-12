# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The overlay of node_classify_eval_compute: every value the build step uses (OMN-19998)."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, model_validator

from omnimarket.models.eval_classify.enum_classify import EnumIntegrationClass


class ModelClassifyEvalOverlay(BaseModel):
    """No key has a default.

    ``section_labels`` maps the start of a ledger section heading to the class its lines carry.
    ``per_class_n`` is how many lines the labelled set holds per class. ``seed`` orders each class's lines
    for the stratified draw. ``min_total`` is the smallest labelled set that is a result. ``rubric`` is the
    instruction an engine is given with each line.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    section_labels: dict[str, EnumIntegrationClass] = Field(min_length=1)
    per_class_n: dict[EnumIntegrationClass, int]
    seed: str = Field(min_length=1)
    min_total: int = Field(ge=1)
    rubric: str = Field(min_length=1)

    @model_validator(mode="after")
    def _every_class_has_a_size(self) -> ModelClassifyEvalOverlay:
        wanted = set(self.section_labels.values())
        if set(self.per_class_n) != wanted:
            raise ValueError(
                "per_class_n must name exactly the classes section_labels maps to"
            )
        if any(n < 1 for n in self.per_class_n.values()):
            raise ValueError("every per_class_n value is at least 1")
        return self


__all__ = ["ModelClassifyEvalOverlay"]
