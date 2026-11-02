# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed request, verdict and result for placing a unit that opens a pull request."""

from enum import StrEnum

from pydantic import BaseModel, ConfigDict

from .model_host_capability import ModelHostCapability


class EnumPrCreateRefusal(StrEnum):
    """Why a candidate host does not take a unit that needs pull request creation."""

    # The host's probe reads ``missing`` for ``scm-pr-create``: its pr-create would refuse (no credential, no login).
    CAPABILITY_MISSING = "capability_missing"
    # The probe could not read, and another candidate host reads ``ok``: an unread host never counts as a pass beside a read one.
    UNKNOWN_WHILE_PEER_READS_OK = "unknown_while_peer_reads_ok"


class ModelHostReading(BaseModel):
    """One candidate host (a name from the declared host table) and the PARITY-CAP lines its probe printed."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    host: str
    capabilities: tuple[ModelHostCapability, ...]


class ModelPrCreatePlacementRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    # The capabilities the unit needs; a unit that does not name ``scm-pr-create`` is admitted on every host.
    needs: frozenset[str]
    hosts: tuple[ModelHostReading, ...]


class ModelPrCreateHostVerdict(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    host: str
    admitted: bool
    # What the probe read for ``scm-pr-create`` on this host: ``ok``, ``missing``, ``unknown``, or ``unread`` (no PARITY-CAP line).
    reading: str
    refusal: EnumPrCreateRefusal | None
    # The probe's detail, and for a missing reading what the host owner adds.
    reason: str


class ModelPrCreatePlacementResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    verdicts: tuple[ModelPrCreateHostVerdict, ...]
