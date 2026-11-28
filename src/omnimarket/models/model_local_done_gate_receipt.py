# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The receipt a local Done gate run leaves for the hosted verifier to read.

A hosted ``node_dod_verify`` run skips every ``local_done_gate`` item: it is not
an authorized executor of them. A local Done gate run of the same ticket at the
same contract revision executes them, and this receipt is what it publishes: the
ticket, the contract revision, each item's verdict, the host and the run.
"""

from __future__ import annotations

import re
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

_OBJECT_ID = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")


class ModelLocalDoneGateItemVerdict(BaseModel):
    """One evidence item's verdict as the local Done gate read it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    item_id: str = Field(..., min_length=1)
    verdict: Literal["VERIFIED", "FAILED", "SKIPPED", "NON_PROBATIVE"]


class ModelLocalDoneGateReceipt(BaseModel):
    """A local_done_gate run's per-item verdicts, bound to one contract revision."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    ticket_id: str = Field(..., min_length=1)
    contract_sha: str = Field(
        ..., description="Full object id of the contract revision."
    )
    items: tuple[ModelLocalDoneGateItemVerdict, ...]
    host: str = Field(..., min_length=1)
    run_id: str = Field(..., min_length=1)

    @model_validator(mode="after")
    def _contract_sha_is_an_object_id(self) -> Self:
        if _OBJECT_ID.fullmatch(self.contract_sha) is None:
            raise ValueError("contract_sha must be a full lowercase hex object id")
        return self


__all__ = ["ModelLocalDoneGateItemVerdict", "ModelLocalDoneGateReceipt"]
