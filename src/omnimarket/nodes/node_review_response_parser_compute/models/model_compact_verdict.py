# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Wire shape of a v2 compact review verdict, validated by ``compact_strict``.

The producing side (reviewer prompt 2.0.0 and its verdict schema) lives in
omniintelligence. These models are the parser's own strict view of that wire
shape; they carry no tolerance, so a reply that does not fit is a failed
review, never a shorter one.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

FAMILIES: tuple[str, ...] = (
    "architecture",
    "security",
    "performance",
    "correctness",
    "completeness",
    "feasibility",
    "testing",
    "style",
)
"""The eight finding families; a catalog code fixes its family, X and R: name one."""

NOTE_MAX_LENGTH: int = 120


class ModelCompactFinding(BaseModel):
    """One compact finding: ``{"c":"T01","p":"src/x.py","l":42,"s":"M"}``."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    c: str = Field(min_length=1, description="Catalog code, 'R:<rule id>' or 'X'.")
    p: str = Field(min_length=1, description="Repository-relative path.")
    line: int = Field(alias="l", ge=0, description="Line number; 0 for file-level.")
    s: Literal["C", "M", "m", "n"] = Field(
        description="Severity: critical, major, minor, nit."
    )
    y: (
        Literal[
            "architecture",
            "security",
            "performance",
            "correctness",
            "completeness",
            "feasibility",
            "testing",
            "style",
        ]
        | None
    ) = Field(
        default=None,
        description="Family; required for 'X' and 'R:' codes, forbidden otherwise.",
    )
    n: str | None = Field(
        default=None,
        max_length=NOTE_MAX_LENGTH,
        description="Short note; required for 'X'.",
    )


class ModelCompactVerdict(BaseModel):
    """A complete compact verdict: the finding list, an overflow flag, a terminal."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    v: str = Field(min_length=1, description="Catalog version the voter was given.")
    f: list[ModelCompactFinding] = Field(
        description="The findings, at most max_findings."
    )
    more: bool = Field(
        description="True when the voter had more defects than it listed."
    )
    done: Literal[True] = Field(description="Terminal marker; absent in a cut reply.")


__all__: list[str] = [
    "FAMILIES",
    "NOTE_MAX_LENGTH",
    "ModelCompactFinding",
    "ModelCompactVerdict",
]
