# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Task shape feature extractor — deterministic extraction of ModelTaskShapeFeatures.

Pure function: no LLM, no I/O beyond what is passed in. Reads git diff stats,
file listings, and ledger query results to produce routing-relevant shape features.
"""

from __future__ import annotations

from collections.abc import Sequence
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class EnumFileType(StrEnum):
    PYTHON = "python"
    TYPESCRIPT = "typescript"
    YAML = "yaml"
    MARKDOWN = "markdown"
    OTHER = "other"


_EXTENSION_MAP: dict[str, EnumFileType] = {
    ".py": EnumFileType.PYTHON,
    ".ts": EnumFileType.TYPESCRIPT,
    ".tsx": EnumFileType.TYPESCRIPT,
    ".yaml": EnumFileType.YAML,
    ".yml": EnumFileType.YAML,
    ".md": EnumFileType.MARKDOWN,
}

_DEFAULT_NOVELTY_SCORE: float = 0.5


class ModelTaskShapeContext(BaseModel):
    """Runtime context supplied to the extractor."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    diff_lines_added: int | None = Field(
        default=None, ge=0, description="Lines added in git diff."
    )
    diff_lines_removed: int | None = Field(
        default=None, ge=0, description="Lines removed in git diff."
    )
    file_paths: tuple[str, ...] = Field(
        default=(), description="Files touched in this task."
    )
    ledger_novelty_score: float | None = Field(
        default=None,
        ge=0.0,
        le=1.0,
        description="Novelty score from ledger query (0=familiar, 1=novel).",
    )


class ModelTaskShapeFeatures(BaseModel):
    """Extracted shape features used by the routing engine."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    diff_size: int = Field(
        default=0, ge=0, description="Total changed lines (added + removed)."
    )
    file_types: frozenset[EnumFileType] = Field(
        default_factory=frozenset,
        description="Distinct file types present in the task.",
    )
    novelty_score: float = Field(
        default=_DEFAULT_NOVELTY_SCORE,
        ge=0.0,
        le=1.0,
        description="Novelty score: 0=familiar, 1=novel. Defaults to 0.5 when unknown.",
    )


def _classify_file_types(file_paths: Sequence[str]) -> frozenset[EnumFileType]:
    types: set[EnumFileType] = set()
    for path in file_paths:
        dot = path.rfind(".")
        if dot == -1:
            types.add(EnumFileType.OTHER)
            continue
        ext = path[dot:].lower()
        types.add(_EXTENSION_MAP.get(ext, EnumFileType.OTHER))
    return frozenset(types)


def extract_task_shape(
    context: ModelTaskShapeContext | None,
) -> ModelTaskShapeFeatures:
    """Return ModelTaskShapeFeatures from the supplied context.

    Returns a default shape when context is None or all fields are absent.
    """
    if context is None:
        return ModelTaskShapeFeatures()

    added = context.diff_lines_added or 0
    removed = context.diff_lines_removed or 0
    diff_size = added + removed

    file_types = _classify_file_types(context.file_paths)

    novelty = (
        context.ledger_novelty_score
        if context.ledger_novelty_score is not None
        else _DEFAULT_NOVELTY_SCORE
    )

    return ModelTaskShapeFeatures(
        diff_size=diff_size,
        file_types=file_types,
        novelty_score=novelty,
    )


__all__: list[str] = [
    "EnumFileType",
    "ModelTaskShapeContext",
    "ModelTaskShapeFeatures",
    "extract_task_shape",
]
