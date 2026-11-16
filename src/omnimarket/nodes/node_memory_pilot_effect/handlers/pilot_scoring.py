# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The frozen scoring rule of the stage-one question set: patterns, never a model judge."""

from __future__ import annotations

import json
import re
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field


class ModelPilotQuestion(BaseModel):
    """One frozen question: groups of alternative patterns that all must match."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str
    category: str
    question: str
    all_of: tuple[tuple[str, ...], ...] = ()
    gold: str


class ModelPilotQuestionSet(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    abstain_regex: str
    scoring: str
    questions: tuple[ModelPilotQuestion, ...] = Field(min_length=1)


HARD_CATEGORIES = frozenset({"multihop", "aggregate"})
#: The frozen question set's own category names for the planted facts and the questions that should get no answer.
PLANTED_CATEGORY = "planted"
ABSTAIN_CATEGORY = "abstain"
ABSTAIN_MAX_CHARS = 600


def load_questions(pilot_dir: Path) -> ModelPilotQuestionSet:
    return ModelPilotQuestionSet.model_validate(
        json.loads((pilot_dir / "questions.json").read_text(encoding="utf-8"))
    )


def score_answer(question: ModelPilotQuestion, answer: str, abstain_regex: str) -> bool:
    """True when the answer is right under the frozen rule."""
    if not question.all_of:
        return (
            bool(re.search(abstain_regex, answer, re.IGNORECASE))
            and len(answer) < ABSTAIN_MAX_CHARS
        )
    return all(
        any(re.search(pattern, answer, re.IGNORECASE) for pattern in group)
        for group in question.all_of
    )
