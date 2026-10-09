# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Requests and results of the merge-sweep lane retry decision (OMN-20676).

The remote-lane runner exits 75 when no lab host qualifies, 77 when the chosen host's Claude
account hit a usage limit and 79 when its login expired. The sweep waits and retries a 75, and
retries a 77 or 79 once on another host.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict

_FROZEN = ConfigDict(frozen=True, extra="forbid")

EXIT_NO_HOST = 75
EXIT_HOST_LIMITED = 77
EXIT_HOST_AUTH_EXPIRED = 79

DEFAULT_RETRIES = 3
RETRIES_CEILING = 6
DEFAULT_RETRY_WAIT_MIN = 5
RETRY_WAIT_CEILING_MIN = 30


class ModelMergeSweepRetryRequest(BaseModel):
    """One lane's latest receipt exit code and what the sweep already spent on it.

    ``retries`` and ``retry_wait_min`` are the sweep's arguments; None reads as the default and
    a value out of range is clamped, as the sweep read its arguments.
    """

    model_config = _FROZEN

    exit_code: int
    waits_used: int = 0
    host_retry_used: bool = False
    retries: int | None = None
    retry_wait_min: int | None = None


class ModelMergeSweepRetryResult(BaseModel):
    """What the sweep does with the lane next."""

    model_config = _FROZEN

    action: Literal["accept", "wait_and_retry", "retry_other_host"]
    wait_min: int | None
    retries: int
    retry_wait_min: int
