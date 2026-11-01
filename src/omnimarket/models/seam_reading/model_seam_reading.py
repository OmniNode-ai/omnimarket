# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One reading of one seam over one window.

``crossings`` counts identities (matched plus mismatched); an unreadable
identity is counted in ``unreadable`` only. A missing expected identity is
mismatched, and an unexpected crossing never raises the count.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Collection, Sequence
from datetime import datetime
from typing import Self

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.models.seam_reading.enum_seam_crossing_state import (
    EnumSeamCrossingState,
)
from omnimarket.models.seam_reading.model_seam_crossing import ModelSeamCrossing
from omnimarket.models.seam_reading.model_seam_window import ModelSeamWindow


class ModelSeamReading(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    seam_id: str
    window: ModelSeamWindow
    crossings: int = Field(ge=0)
    mismatched: int = Field(ge=0)
    unreadable: int = Field(ge=0)
    examples: tuple[str, ...] = ()
    read_at: datetime
    expected: int | None = Field(default=None, ge=0)
    unexpected: int = Field(default=0, ge=0)
    source_cursor: str | None = None

    @property
    def has_identity_evidence(self) -> bool:
        return self.expected is not None and bool(self.source_cursor)

    @classmethod
    def from_crossings(
        cls,
        seam_id: str,
        window: ModelSeamWindow,
        crossings: Sequence[ModelSeamCrossing],
        read_at: datetime,
        *,
        expected_identities: Collection[str] | None,
        source_cursor: str | None,
    ) -> Self:
        key_counts = Counter(c.key for c in crossings)
        dupes = [key for key, count in key_counts.items() if count > 1]
        if dupes:
            raise ValueError(
                f"seam {seam_id}: repeated crossing key(s): {', '.join(sorted(dupes))}"
            )

        expected = (
            frozenset(expected_identities) if expected_identities is not None else None
        )
        included = [c for c in crossings if expected is None or c.key in expected]
        mismatched = [
            c.key for c in included if c.state is EnumSeamCrossingState.MISMATCHED
        ]
        if expected is not None:
            mismatched.extend(sorted(expected.difference(key_counts)))
        unreadable = sum(
            1 for c in included if c.state is EnumSeamCrossingState.UNREADABLE
        )
        return cls(
            seam_id=seam_id,
            window=window,
            crossings=(len(expected) if expected is not None else len(crossings))
            - unreadable,
            mismatched=len(mismatched),
            unreadable=unreadable,
            examples=tuple(mismatched),
            read_at=read_at,
            expected=len(expected) if expected is not None else None,
            unexpected=len(crossings) - len(included),
            source_cursor=source_cursor or None,
        )
