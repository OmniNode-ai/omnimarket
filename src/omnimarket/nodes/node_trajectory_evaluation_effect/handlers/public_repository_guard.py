# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Anonymous public-repository read, failing closed (OMN-20087).

The repository is validated with ``re.fullmatch`` before the URL is built, which
refuses ``../x`` and ``x/..``. Redirects are not followed. A 404, or a 200 whose
``private`` is not ``false``, is not public; a 403 or 429 carrying
``X-RateLimit-Remaining: 0`` or ``Retry-After`` is rate limited; any other
status, a timeout or a non-JSON body is unresolved.
"""

from __future__ import annotations

import logging
import re
from enum import StrEnum
from urllib.parse import urlsplit
from uuid import UUID

import httpx

from omnimarket.config.service_endpoints import GITHUB_REST_URL

_LOG = logging.getLogger(__name__)

_REPOSITORY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]*/(?!\.\.?$)[A-Za-z0-9_.-]+$")


def is_valid_repository(repository: object) -> bool:
    return isinstance(repository, str) and _REPOSITORY.fullmatch(repository) is not None


class EnumGuardVerdict(StrEnum):
    PUBLIC = "public"
    NOT_PUBLIC = "not_public"
    RATE_LIMITED = "rate_limited"
    UNRESOLVED = "unresolved"


class PublicRepositoryGuard:
    """Reads ``GET {GITHUB_REST_URL}/repos/{owner}/{name}`` anonymously."""

    def __init__(
        self,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout_seconds: float = 5.0,
        base_url: str = GITHUB_REST_URL,
    ) -> None:
        self._transport = transport
        self._timeout = timeout_seconds
        self._base_url = base_url.rstrip("/")

    async def check(self, repository: str, correlation_id: UUID) -> EnumGuardVerdict:
        if not is_valid_repository(repository):
            return EnumGuardVerdict.UNRESOLVED
        url = f"{self._base_url}/repos/{repository}"
        host = urlsplit(url).hostname or ""
        try:
            async with httpx.AsyncClient(
                transport=self._transport,
                timeout=self._timeout,
                follow_redirects=False,
            ) as client:
                response = await client.get(
                    url, headers={"Accept": "application/vnd.github+json"}
                )
        except httpx.HTTPError as exc:
            _LOG.info(
                "outbound_call operation=repository_read correlation_id=%s host=%s status=%s",
                correlation_id,
                host,
                type(exc).__name__,
            )
            return EnumGuardVerdict.UNRESOLVED
        _LOG.info(
            "outbound_call operation=repository_read correlation_id=%s host=%s status=%s",
            correlation_id,
            host,
            response.status_code,
        )
        status = response.status_code
        if status == 404:
            return EnumGuardVerdict.NOT_PUBLIC
        if status in (403, 429):
            limited = (
                response.headers.get("X-RateLimit-Remaining") == "0"
                or "Retry-After" in response.headers
            )
            return (
                EnumGuardVerdict.RATE_LIMITED
                if limited
                else EnumGuardVerdict.UNRESOLVED
            )
        if status != 200:
            return EnumGuardVerdict.UNRESOLVED
        try:
            body = response.json()
        except ValueError:
            return EnumGuardVerdict.UNRESOLVED
        if not isinstance(body, dict):
            return EnumGuardVerdict.UNRESOLVED
        if body.get("private") is False:
            return EnumGuardVerdict.PUBLIC
        return EnumGuardVerdict.NOT_PUBLIC
