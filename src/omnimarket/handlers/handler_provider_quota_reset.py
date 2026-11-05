# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Shared quota-reset handler used by the delegation call effect and local port."""

from __future__ import annotations

import re
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

from omnimarket.models.delegation.model_provider_quota_reset import (
    ModelProviderQuotaResetRequest,
    ModelProviderQuotaResetResult,
)

# z.ai states the reset inline: "Your limit will reset at 2026-08-30 20:32:52".
_RESET_PATTERN = re.compile(
    r"reset\s+at\s+(\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2})", re.IGNORECASE
)

# Gemini states a relative delay instead: "Please retry in 27.024259969s."
# (OMN-16932). Same information, different shape, so it needs its own
# contract-selectable ``reset_from`` mode rather than being crammed into the
# absolute-instant parser above.
_RETRY_DELAY_PATTERN = re.compile(
    r"retry\s+in\s+(\d+(?:\.\d+)?)\s*s(?:econds?)?\b", re.IGNORECASE
)


def error_metadata(body: dict[str, Any] | None) -> dict[str, Any]:
    error = body.get("error") if isinstance(body, dict) else None
    metadata = error.get("metadata") if isinstance(error, dict) else None
    return metadata if isinstance(metadata, dict) else {}


def parse_rate_limit_reset(
    body: dict[str, Any] | None,
    headers: Mapping[str, str] | None,
    *,
    reference: datetime,
) -> datetime | None:
    """Read a future epoch-millisecond reset from either provider header surface.

    OpenRouter includes its quota headers inside the error body. Invalid or
    stale values cannot produce an already-expired block and immediate retry.
    """
    nested = error_metadata(body).get("headers")
    for source in (headers, nested):
        if not isinstance(source, Mapping):
            continue
        for name, value in source.items():
            if str(name).lower() != "x-ratelimit-reset":
                continue
            try:
                reset = datetime.fromtimestamp(float(value) / 1000, tz=UTC)
            except (TypeError, ValueError, OverflowError, OSError):
                continue
            if reset > reference:
                return reset
    return None


def parse_reset_instant(message: str) -> datetime | None:
    """Parse the reset instant the provider states inline.

    The provider reports a naive wall-clock stamp with no offset. It is read as
    UTC so the value is comparable and unambiguous; the cost of being wrong is
    bounded either way (a slightly early retry re-trips the same 429, a
    slightly late one waits a little longer).
    """
    match = _RESET_PATTERN.search(message)
    if not match:
        return None
    stamp = match.group(1).replace(" ", "T")
    try:
        parsed = datetime.fromisoformat(stamp)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def parse_retry_delay(message: str, *, reference: datetime) -> datetime | None:
    """Parse a provider-stated relative retry delay into an absolute instant.

    Gemini reports "Please retry in 27.024259969s." rather than a wall-clock
    reset. Reading it against ``reference`` (the moment the 429 was observed)
    keeps the resulting instant comparable with the absolute-stamp path, so both
    ``reset_from`` modes produce the same downstream shape.
    """
    match = _RETRY_DELAY_PATTERN.search(message)
    if not match:
        return None
    seconds = float(match.group(1))
    if seconds < 0:
        return None
    return reference + timedelta(seconds=seconds)


class HandlerProviderQuotaReset:
    """Read the contract-selected reset surface before the classifier's fallback."""

    def handle(
        self, request: ModelProviderQuotaResetRequest
    ) -> ModelProviderQuotaResetResult:
        reset = None
        if request.reset_from == "message_reset_timestamp":
            reset = parse_reset_instant(request.message)
        elif request.reset_from == "message_retry_delay":
            reset = parse_retry_delay(request.message, reference=request.reference)
        elif request.reset_from == "rate_limit_reset_milliseconds":
            reset = parse_rate_limit_reset(
                request.body, request.headers, reference=request.reference
            )
        return ModelProviderQuotaResetResult(reset_at=reset)
