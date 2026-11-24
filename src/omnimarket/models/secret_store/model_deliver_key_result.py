# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The result of delivering one key into a host env file (OMN-24611).

There is deliberately NO field that can hold the value: the value is read from
the store and written straight into the env file, and stays in both. A result
carries the outcome, the env NAME, the file's path under the home and, only
when the file was written or already held the line, the value's length -- the
one fact about it that may be printed.
"""

from __future__ import annotations

from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel, ConfigDict


class EnumDeliverKeyOutcome(StrEnum):
    """Every way a delivery can end. Only the first two leave the file in place."""

    DELIVERED = "delivered"
    #: The file already held exactly the declared line; it was not rewritten.
    UNCHANGED = "unchanged"
    #: The contract's ``env_file_deliveries`` declares no such id, or its
    #: ``key_id`` is no ``generated_keys`` entry. Nothing was read or written.
    NOT_DECLARED = "not_declared"
    #: The store could not be asked or holds no such key. Nothing was written.
    KEY_UNREADABLE = "key_unreadable"
    #: The stored value is not in the shape systemd's ``EnvironmentFile=``
    #: parses without quoting. Nothing was written.
    VALUE_UNSAFE = "value_unsafe"
    #: The file could not be written; the detail names the exception class only.
    WRITE_FAILED = "write_failed"


class ModelDeliverKeyResult(BaseModel):
    """What happened to one delivery. Never the value."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    outcome: EnumDeliverKeyOutcome
    #: The NAME of the environment variable the file declares, never a value.
    env_name: str | None = None
    #: The file's path under the user's home, for receipts.
    home_relative_path: str | None = None
    #: Length of the delivered value. Set only when delivered or unchanged.
    key_length: int | None = None
    #: Why a delivery failed, naming ids, keys, files and exception types only.
    detail: str | None = None
    correlation_id: UUID | None = None


__all__ = ["EnumDeliverKeyOutcome", "ModelDeliverKeyResult"]
