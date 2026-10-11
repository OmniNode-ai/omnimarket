# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The GitHub reads of node_github_schedule_observer_effect (OMN-20803).

Every read goes through the shared landing transport, so a response arrives
whole with its headers. Quota is read from those headers only and a call is
refused, with no request made, once the last reading for its resource is under
the contract's floor. A read that fails says why, typed as the liveness reason
it maps to; nothing here turns an unreadable source into an empty one.
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import NamedTuple

from omnimarket.events.pr_landing_github.model_github_quota_reading import (
    ModelGithubQuotaHeadersMissingError,
    ModelGithubQuotaReading,
)
from omnimarket.github_landing.github_landing_transport import (
    GithubLandingTransportError,
)
from omnimarket.github_landing.model_github_http_exchange import (
    ModelGithubHttpRequest,
    ModelGithubHttpResponse,
)
from omnimarket.models.github_quota_floor import (
    ModelGithubQuotaFloor,
)
from omnimarket.models.liveness.model_automation_liveness import (
    EnumAutomationLivenessReason,
)
from omnimarket.nodes.node_github_schedule_observer_effect.protocols.protocol_github_schedule_transport import (
    ProtocolGithubScheduleTransport,
)

#: Page size every list request asks for; a shorter page is the last one.
PAGE_SIZE = 100
_QUERY_SPLIT = "?"
_RATE_LIMIT_MARKERS = ("rate limit", "abuse detection")


class ScheduleReadError(RuntimeError):
    """A read failed; ``reason`` is the UNOBSERVABLE reason it maps to."""

    def __init__(
        self,
        reason: EnumAutomationLivenessReason,
        detail: str,
        *,
        http_status: int | None = None,
    ) -> None:
        super().__init__(detail)
        self.reason = reason
        self.detail = detail
        self.http_status = http_status


def _without_query(path: str) -> str:
    return path.split(_QUERY_SPLIT, 1)[0]


class Page(NamedTuple):
    """One page of a listing: its items and the total the body reports, if any."""

    items: list[dict[str, object]]
    total_count: int | None


class GithubScheduleReader:
    """Read GitHub through one transport, honouring the header quota floor."""

    def __init__(
        self,
        transport_factory: Callable[[], Awaitable[ProtocolGithubScheduleTransport]],
        *,
        quota_floor: ModelGithubQuotaFloor,
        identity: str,
        max_pages: int,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._transport_factory = transport_factory
        self._transport: ProtocolGithubScheduleTransport | None = None
        self._floor = quota_floor
        self._identity = identity
        self._max_pages = max_pages
        self._clock = clock
        self._readings: dict[str, ModelGithubQuotaReading] = {}
        self.requests_sent = 0

    def _floor_reading(self, resource: str) -> ModelGithubQuotaReading | None:
        reading = self._readings.get(resource)
        if reading is None or self._clock() >= reading.reset:
            return None
        if self._floor.refusal(reading) is None:
            return None
        return reading

    async def _get_transport(self) -> ProtocolGithubScheduleTransport:
        if self._transport is None:
            try:
                self._transport = await self._transport_factory()
            except (RuntimeError, ValueError) as exc:
                raise ScheduleReadError(
                    EnumAutomationLivenessReason.EVIDENCE_UNREADABLE,
                    f"no GitHub credential: {exc}",
                ) from None
        return self._transport

    async def get(self, path: str) -> ModelGithubHttpResponse:
        """GET one path; return the response only for a 2xx status.

        Raises:
            ScheduleReadError: READ_INCOMPLETE when the quota floor or a rate
                limit stops the read; EVIDENCE_UNREADABLE for every other failure.
        """
        under = self._floor_reading("core")
        if under is not None:
            raise ScheduleReadError(
                EnumAutomationLivenessReason.READ_INCOMPLETE,
                f"core quota remaining {under.remaining} is under the contract "
                f"floor until {under.reset}; no call made for {_without_query(path)}",
            )
        transport = await self._get_transport()
        try:
            response = await transport.send(
                ModelGithubHttpRequest(method="GET", path=path)
            )
        except (GithubLandingTransportError, OSError) as exc:
            raise ScheduleReadError(
                EnumAutomationLivenessReason.EVIDENCE_UNREADABLE, str(exc)
            ) from None
        self.requests_sent += 1
        try:
            reading = ModelGithubQuotaReading.from_response_headers(
                response.headers, identity=self._identity
            )
        except ModelGithubQuotaHeadersMissingError as exc:
            if not self._readings:
                raise ScheduleReadError(
                    EnumAutomationLivenessReason.EVIDENCE_UNREADABLE,
                    f"HTTP {response.status} from {_without_query(path)}: {exc}",
                ) from None
        else:
            self._readings[reading.resource] = reading
        if 200 <= response.status < 300:
            return response
        message = response.message()
        limited = response.status in (403, 429) and (
            response.header("x-ratelimit-remaining") == "0"
            or response.header("retry-after") is not None
            or any(marker in message.lower() for marker in _RATE_LIMIT_MARKERS)
        )
        reason = (
            EnumAutomationLivenessReason.READ_INCOMPLETE
            if limited
            else EnumAutomationLivenessReason.EVIDENCE_UNREADABLE
        )
        raise ScheduleReadError(
            reason,
            f"HTTP {response.status} from {_without_query(path)}: {message[:200]}",
            http_status=response.status,
        )

    async def pages(self, path: str, *, key: str | None) -> AsyncIterator[Page]:
        """Yield each page's items; ``key`` names the list in an object body.

        A body that is a bare array is read from ``key=None``. The iteration
        raises READ_INCOMPLETE when the page limit is reached with more pages
        still to read, so a truncated listing is never taken for a whole one.
        A consumer that stops early closes the iterator without that error.
        """
        separator = "&" if _QUERY_SPLIT in path else _QUERY_SPLIT
        for page in range(1, self._max_pages + 1):
            response = await self.get(f"{path}{separator}page={page}")
            items = _items(response, key, path)
            total = (response.body or {}).get("total_count")
            yield Page(items, total if isinstance(total, int) else None)
            if not _has_next_page(response, len(items)):
                return
        raise ScheduleReadError(
            EnumAutomationLivenessReason.READ_INCOMPLETE,
            f"{_without_query(path)} has more than {self._max_pages} pages",
        )

    async def collect(self, path: str, *, key: str | None) -> list[dict[str, object]]:
        """Read every page of a listing.

        Raises READ_INCOMPLETE when the body reports more items than the pages
        returned: GitHub lists only the first 1000 results of a filtered query.
        """
        collected: list[dict[str, object]] = []
        total: int | None = None
        async for page in self.pages(path, key=key):
            collected.extend(page.items)
            total = page.total_count
        if total is not None and len(collected) < total:
            raise ScheduleReadError(
                EnumAutomationLivenessReason.READ_INCOMPLETE,
                f"{_without_query(path)} lists {len(collected)} of {total} items",
            )
        return collected


def _items(
    response: ModelGithubHttpResponse, key: str | None, path: str
) -> list[dict[str, object]]:
    body = response.body or {}
    raw = body.get(key or "value")
    if not isinstance(raw, list) or not all(isinstance(i, dict) for i in raw):
        raise ScheduleReadError(
            EnumAutomationLivenessReason.EVIDENCE_UNREADABLE,
            f"unexpected body shape from {_without_query(path)}",
        )
    return [{str(k): v for k, v in item.items()} for item in raw]


def _has_next_page(response: ModelGithubHttpResponse, item_count: int) -> bool:
    link = response.header("link")
    if link is not None:
        return 'rel="next"' in link
    return item_count >= PAGE_SIZE


__all__: list[str] = ["PAGE_SIZE", "GithubScheduleReader", "ScheduleReadError"]
