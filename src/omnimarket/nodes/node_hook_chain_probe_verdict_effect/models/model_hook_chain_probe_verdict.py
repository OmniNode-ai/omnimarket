# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed payloads for the hook-chain probe verdict consumer."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class ModelHookChainProbeOutcome(BaseModel):
    """Either terminal event of the probe, as this node's input.

    The ``-completed`` event carries the probe result; the ``-failed`` event
    carries an error. Every field is optional and ``extra="ignore"`` so both
    shapes validate; a payload without ``chain_complete`` true is never healthy.
    """

    model_config = ConfigDict(frozen=True, extra="ignore")

    correlation_id: str | None = None
    chain_complete: bool = False
    failed_leg: str | None = None
    primary_blocker: str | None = None
    error: str | None = None


class ModelHookChainHealthRecorded(BaseModel):
    """What leaves the node: the recorded hook-chain health."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: str | None
    healthy: bool
    failed_leg: str | None = None
    primary_blocker: str | None = None
    error: str | None = None


__all__ = ["ModelHookChainHealthRecorded", "ModelHookChainProbeOutcome"]
