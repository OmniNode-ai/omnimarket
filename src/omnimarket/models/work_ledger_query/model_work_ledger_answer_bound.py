# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The size bound every answer of the work ledger read node stays under.

The answer travels as one record on the completed topic, and the producer
refuses a record over its ``max_request_size``. The unbounded
``newest-per-lane`` answer measured 73 MB on the dev lane and the broker
refused it, so a bound the node checks before it answers is declared in the
contract and validated here: the bound plus the envelope's headroom must fit
under the broker's limit, or the contract does not load.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ModelWorkLedgerAnswerBound(BaseModel):
    """``work_ledger_query.answer_bound`` of the node contract."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    broker_max_request_size: int = Field(gt=0)
    envelope_headroom_bytes: int = Field(ge=0)
    max_answer_bytes: int = Field(gt=0)

    @model_validator(mode="after")
    def _fits_under_the_broker(self) -> ModelWorkLedgerAnswerBound:
        if self.max_answer_bytes + self.envelope_headroom_bytes > (
            self.broker_max_request_size
        ):
            raise ValueError(
                f"max_answer_bytes {self.max_answer_bytes} plus envelope_headroom_bytes "
                f"{self.envelope_headroom_bytes} exceeds broker_max_request_size "
                f"{self.broker_max_request_size}"
            )
        return self


__all__ = ["ModelWorkLedgerAnswerBound"]
