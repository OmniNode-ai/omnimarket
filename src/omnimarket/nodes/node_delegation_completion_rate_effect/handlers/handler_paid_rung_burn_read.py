# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerPaidRungBurnRead: how fast the hosted-model credit goes (OMN-23756).

Reads the provider's own credits through the injected reader, the paid-rung calls through the injected source, and builds
the reading with ``build_paid_rung_burn_reading``. It writes nothing. Credits that cannot be read are a failed run (the
balance and the spend are the reading); calls that cannot be read leave the reading in place and say per-model is unread.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime

from omnimarket.handlers.routing.paid_rungs import build_paid_rung_burn_reading
from omnimarket.models.delegation.model_paid_rung_burn import (
    ModelPaidRungCallRecord,
    ModelProviderCreditsReading,
)
from omnimarket.models.delegation.model_paid_rungs_overlay import (
    ModelPaidRungsOverlay,
    ModelPaidRungSpendSource,
)
from omnimarket.nodes.node_delegation_completion_rate_effect.models.model_paid_rung_burn_run import (
    ModelPaidRungBurnRunResult,
)
from omnimarket.nodes.node_delegation_completion_rate_effect.protocols import (
    ProtocolPaidRungCallSource,
)

CreditsReader = Callable[[ModelPaidRungSpendSource], ModelProviderCreditsReading]


class HandlerPaidRungBurnRead:
    """EFFECT handler: reads credits and calls through its ports and takes its clock as a parameter."""

    def __init__(
        self,
        *,
        paid: ModelPaidRungsOverlay,
        credits_reader: CreditsReader,
        calls: ProtocolPaidRungCallSource,
        clock: Callable[[], datetime],
    ) -> None:
        self._paid = paid
        self._credits_reader = credits_reader
        self._calls = calls
        self._clock = clock

    def handle(self) -> ModelPaidRungBurnRunResult:
        now = self._clock()
        try:
            credits = self._credits_reader(self._paid.spend_source)
        except Exception as exc:  # any credential, transport or shape failure
            return ModelPaidRungBurnRunResult(
                status="failed",
                read_at=now,
                reading=None,
                per_model_unread_reason=None,
                failure=f"credits-unreadable-{type(exc).__name__}",
            )
        unread: str | None = None
        calls: tuple[ModelPaidRungCallRecord, ...] = ()
        if self._paid.enabled_at is None:
            unread = "switch-off-no-calls-since-switch-on"
        else:
            read = self._calls.read(
                self._paid.enabled_at,
                tuple(rung.slug for rung in self._paid.rungs),
            )
            if read.error is not None:
                unread = read.error
            else:
                calls = read.calls
        reading = build_paid_rung_burn_reading(self._paid, credits, calls, now=now)
        return ModelPaidRungBurnRunResult(
            status="read",
            read_at=now,
            reading=reading,
            per_model_unread_reason=unread,
            failure=None,
        )


__all__ = ["HandlerPaidRungBurnRead"]
