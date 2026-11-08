# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed models of the J4 join test (OMN-22912): overlays to secrets, plan section 4.1.

J4 joins piece 4 (contracts, overlays, injected dependencies) to piece 5 (secrets): a node
resolves a key through its overlay from the store, and a scan finds no key value in the node's
code, its log or the ledger rows it wrote. Every model here holds outcomes, names and counts only,
never a value, a digest of one or a length.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.models.model_reading import EnumReadingOutcome, EnumReadingVerdict
from omnimarket.models.model_secret_reading import EnumSecretProbeOutcome


class EnumJ4Surface(StrEnum):
    """The three places J4 says a key must never appear."""

    CODE = "code"
    LOG = "log"
    LEDGER = "ledger"


class ModelJoinJ4Command(BaseModel):
    """One run of the join: under one correlation id, with or without writing the row."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: str = Field(min_length=1)
    write: bool = False


class ModelJ4Scan(BaseModel):
    """What the scan read and where it found the consumer's value.

    ``exposed`` names the surface kinds that held the value, never the value. ``dummy_found`` is
    the control: the planted dummy must be found on its surface, or the scan is blind and the
    join reads NOT WORKING.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    code_files: int = 0
    log_lines: int = 0
    ledger_rows: int = 0
    unreadable: tuple[str, ...] = ()
    exposed: tuple[EnumJ4Surface, ...] = ()
    dummy_found: bool = False


class ModelJoinJ4Result(BaseModel):
    """The join's reading of one run."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: str
    status: Literal["completed", "failed"]
    verdict: EnumReadingVerdict | None = None
    outcome: EnumReadingOutcome | None = None
    reasons: tuple[str, ...] = ()
    consumer: str | None = None
    resolve_outcome: EnumSecretProbeOutcome | None = None
    scan: ModelJ4Scan | None = None
    consumer_chain: Literal["success", "failure", "not-run"] = "not-run"
    scan_chain: Literal["success", "failure", "upstream-refusal", "not-run"] = "not-run"
    ledger: str = "not-written"
    row: str | None = None
    failure: str | None = None


__all__ = [
    "EnumJ4Surface",
    "ModelJ4Scan",
    "ModelJoinJ4Command",
    "ModelJoinJ4Result",
]
