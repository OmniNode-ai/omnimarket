# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""What the record journal keeps (OMN-23757): a pure decision, never an I/O.

The journal answers one question, "is this tick already recorded", and the step publishes each record
before it journals it, so the broker holds the history. The overlay declares how many of the newest
records the journal keeps (record_journal_max_records); this module decides, from what a bounded tail
read found, whether the journal is read whole, how many records a read parses, and whether the
recorder must trim. The recorder only executes that decision. With no declared value the decision is
the unbounded one the step always had, and it names the missing key so the pass line can say so.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

RETENTION_KEY = "record_journal_max_records"


class EnumJournalRetention(StrEnum):
    BOUNDED = "bounded"
    UNSET = "unset"


class ModelJournalRetentionDecision(BaseModel):
    """The read bound and the trim verdict for one journal state."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    retention: EnumJournalRetention
    read_limit: int | None = Field(
        description="How many of the newest records a read may parse; None reads every record."
    )
    trim: bool = Field(
        description="True when the journal holds more than read_limit records and the recorder rewrites it to the newest read_limit."
    )
    missing_key: str | None = Field(
        default=None, description="The overlay key that is absent, when unbounded."
    )


def decide_journal_retention(
    max_records: int | None, *, records_found: int = 0
) -> ModelJournalRetentionDecision:
    """The decision for a journal in which a tail read of max_records + 1 found ``records_found`` records.

    A tail read never returns more than max_records + 1 records, so ``records_found`` above max_records
    means the journal holds more than it may: the recorder rewrites it to the newest max_records, which
    drops one record per pass in steady state and the whole legacy excess on the first pass.
    """
    if max_records is None:
        return ModelJournalRetentionDecision(
            retention=EnumJournalRetention.UNSET,
            read_limit=None,
            trim=False,
            missing_key=RETENTION_KEY,
        )
    return ModelJournalRetentionDecision(
        retention=EnumJournalRetention.BOUNDED,
        read_limit=max_records,
        trim=records_found > max_records,
    )


def retention_line(
    decision: ModelJournalRetentionDecision, *, trimmed: bool
) -> dict[str, object]:
    """The typed fields a pass line carries about the journal."""
    if decision.retention is EnumJournalRetention.UNSET:
        return {
            "journal_retention": decision.retention.value,
            "journal_retention_missing_key": decision.missing_key,
        }
    return {
        "journal_retention": decision.retention.value,
        "journal_max_records": decision.read_limit,
        "journal_trimmed": trimmed,
    }


__all__: list[str] = [
    "RETENTION_KEY",
    "EnumJournalRetention",
    "ModelJournalRetentionDecision",
    "decide_journal_retention",
    "retention_line",
]
