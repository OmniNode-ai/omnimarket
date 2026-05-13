# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Request-level quality contract primitives for delegation."""

from __future__ import annotations

import re
from typing import Literal

EnumQualityContractMode = Literal["extend_task_class", "replace_task_class"]

SUPPORTED_ACCEPTANCE_CRITERIA = frozenset(
    {
        "exactly_two_sentences",
        "plain_text_only",
        "response_non_empty",
    }
)
MAX_WORDS_PER_SENTENCE_RE = re.compile(r"^max_words_per_sentence_([1-9]\d*)$")


def validate_acceptance_criteria(criteria: tuple[str, ...]) -> tuple[str, ...]:
    """Validate request-level quality criteria before they enter dispatch."""
    unsupported = [
        item
        for item in criteria
        if item not in SUPPORTED_ACCEPTANCE_CRITERIA
        and not MAX_WORDS_PER_SENTENCE_RE.match(item)
    ]
    if unsupported:
        joined = ", ".join(sorted(unsupported))
        raise ValueError(f"unsupported acceptance criteria: {joined}")
    return criteria


__all__: list[str] = [
    "MAX_WORDS_PER_SENTENCE_RE",
    "SUPPORTED_ACCEPTANCE_CRITERIA",
    "EnumQualityContractMode",
    "validate_acceptance_criteria",
]
