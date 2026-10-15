# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One ledger row as the markdown projection and its repair source exchange it (OMN-20275).

Shared so the projection node and any ledger-of-record reader build and read the same
row without importing the node's private package. A record is ``(ledger_id,
ledger_seq)``; ``row_id`` (the sha256 of the raw row) is a content check and never the
identity: two distinct rows can carry the same text.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, model_validator

from omnimarket.events.model_ledger_row_event import work_ledger_row_id


class ModelWorkLedgerRenderRow(BaseModel):
    """One row as the projection needs it: the judge-assigned seq, the hash and the text.

    Built from a row event payload (extra envelope fields are ignored) or from the
    ledger of record's answer to a repair read.
    """

    model_config = ConfigDict(frozen=True, extra="ignore")

    ledger_id: str = Field(min_length=1)
    ledger_seq: int = Field(ge=1)
    row_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    raw_row: str = Field(min_length=1)

    @model_validator(mode="after")
    def _row_id_is_the_hash_of_the_text(self) -> ModelWorkLedgerRenderRow:
        if self.raw_row != self.raw_row.strip():
            raise ValueError("raw_row must be the normalized source row")
        if work_ledger_row_id(self.raw_row) != self.row_id:
            raise ValueError("row_id does not match raw_row")
        return self


__all__: list[str] = ["ModelWorkLedgerRenderRow"]
