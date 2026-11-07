# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed models of node_secret_reading_effect, the piece 5 (secrets) reading (OMN-22659).

A consumer probe resolves one consumer's key from the store by its handle. The value lives only
in ``ModelResolvedSecret``, in process, excluded from serialization; every other model holds
outcomes, names and counts only, never a value, a digest of one or a length.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr

from omnimarket.models.model_reading import EnumReadingOutcome, EnumReadingVerdict


class EnumSecretProbeOutcome(StrEnum):
    """How one consumer's resolve ended. Only ``resolved`` passes."""

    RESOLVED = "resolved"
    #: The key exists and the identity read it, but it holds nothing.
    EMPTY = "empty"
    NOT_FOUND = "not_found"
    #: The store refused this consumer's identity or scope.
    REFUSED = "refused"
    UNREACHABLE = "unreachable"
    NOT_CONFIGURED = "not_configured"
    MISCONFIGURED = "misconfigured"
    FAILED = "failed"

    @property
    def verdict(self) -> EnumReadingVerdict:
        """A consumer that cannot get its key, for any reason, fails its probe."""
        if self is EnumSecretProbeOutcome.RESOLVED:
            return EnumReadingVerdict.WORKING
        return EnumReadingVerdict.NOT_WORKING

    @property
    def store_unread(self) -> bool:
        """The store itself could not be read, as opposed to answering about the key."""
        return self in _STORE_UNREAD


_STORE_UNREAD = frozenset(
    {
        EnumSecretProbeOutcome.UNREACHABLE,
        EnumSecretProbeOutcome.NOT_CONFIGURED,
        EnumSecretProbeOutcome.MISCONFIGURED,
        EnumSecretProbeOutcome.FAILED,
    }
)


class ModelSecretHandle(BaseModel):
    """Where one consumer's key lives: a folder and a key name, never a value."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    consumer: str = Field(min_length=1)
    folder: str = Field(min_length=1, pattern=r"^/")
    key: str = Field(min_length=1)


class ModelResolvedSecret(BaseModel):
    """One resolve, in process only: the value is excluded from every serialization."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    outcome: EnumSecretProbeOutcome
    value: SecretStr | None = Field(default=None, exclude=True, repr=False)


class ModelSecretReadingCommand(BaseModel):
    """Which consumers to probe (none means every declared one), and whether to write the row."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: str = Field(min_length=1)
    consumers: tuple[str, ...] = ()
    write: bool = False


class ModelSecretConsumerProbe(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    consumer: str
    folder: str
    key: str
    outcome: EnumSecretProbeOutcome
    verdict: EnumReadingVerdict

    @property
    def failed(self) -> bool:
        return self.verdict is not EnumReadingVerdict.WORKING


class ModelExposureScan(BaseModel):
    """The exposure probe: each resolved value searched for on each declared surface.

    ``exposures`` counts (consumer, surface) pairs where a value was found; the surfaces and
    consumers are named, the value never is. ``dummy_found`` is the control: the planted dummy
    must be found, or the scanner is blind and the reading fails.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    surfaces: int = 0
    unreadable: tuple[str, ...] = ()
    exposures: tuple[str, ...] = ()
    dummy_found: bool = False


class ModelSecretReadingResult(BaseModel):
    """The piece 5 reading of one run: one verdict over every probe."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: str
    status: Literal["completed", "failed"]
    verdict: EnumReadingVerdict | None = None
    outcome: EnumReadingOutcome | None = None
    reasons: tuple[str, ...] = ()
    probes: tuple[ModelSecretConsumerProbe, ...] = ()
    scan: ModelExposureScan | None = None
    open_exposures: int = 0
    row_key: str | None = None
    ledger: str = "not-written"
    row: str | None = None
    failure: str | None = None


__all__ = [
    "EnumSecretProbeOutcome",
    "ModelExposureScan",
    "ModelResolvedSecret",
    "ModelSecretConsumerProbe",
    "ModelSecretHandle",
    "ModelSecretReadingCommand",
    "ModelSecretReadingResult",
]
