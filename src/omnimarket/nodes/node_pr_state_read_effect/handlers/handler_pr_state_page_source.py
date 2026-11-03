# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Pages of the ``pr_state`` exposure over ``GET /projection/{topic}``.

This is the only request the read surface makes: it asks the projection API
and nothing else. The contract declares the HTTP transport this handler uses.
"""

import httpx
from pydantic import ValidationError

from omnimarket.models.pr_state.model_pr_state_page_fetch import (
    ModelPrStatePageRequest,
    ModelPrStatePageResult,
)
from omnimarket.models.pr_state.model_pr_state_projection_page import (
    ModelPrStateProjectionPage,
    ModelPrStateProjectionRefusal,
)

UNREACHABLE = "projection_unreachable"
REFUSED = "projection_refused"
MALFORMED = "projection_page_malformed"
UNCONFIGURED = "projection_client_unset"

DEFAULT_REQUEST_TIMEOUT_SECONDS = 10.0


def open_projection_client(base_url: str) -> httpx.Client:
    """The client for the projection API at ``base_url``."""
    return httpx.Client(base_url=base_url.rstrip("/"))


class HandlerPrStatePageSource:
    """Reads one page of an exposure through the client it is given.

    The `onex pr-state` command gives it the client for the projection API. A
    handler built with no client refuses every read with ``UNCONFIGURED``
    rather than guess an address.
    """

    def __init__(
        self,
        client: httpx.Client | None = None,
        timeout_seconds: float = DEFAULT_REQUEST_TIMEOUT_SECONDS,
    ) -> None:
        self._client = client
        self._timeout_seconds = timeout_seconds

    def handle(self, request: ModelPrStatePageRequest) -> ModelPrStatePageResult:
        if self._client is None:
            return ModelPrStatePageResult(
                result=ModelPrStateProjectionRefusal(status_code=0, error=UNCONFIGURED)
            )
        return ModelPrStatePageResult(
            result=self._fetch(self._client, request.topic, request.since)
        )

    def _fetch(
        self, client: httpx.Client, topic: str, since: str | None
    ) -> ModelPrStateProjectionPage | ModelPrStateProjectionRefusal:
        params = {} if since is None else {"since": since}
        try:
            response = client.get(
                f"/projection/{topic}",
                params=params,
                timeout=self._timeout_seconds,
            )
        except httpx.HTTPError:
            return ModelPrStateProjectionRefusal(status_code=0, error=UNREACHABLE)
        try:
            body = response.json()
        except ValueError:
            body = None
        if response.status_code != 200:
            error = body.get("error") if isinstance(body, dict) else None
            return ModelPrStateProjectionRefusal(
                status_code=response.status_code,
                error=error if isinstance(error, str) else REFUSED,
            )
        try:
            rows = body["rows"]
            return ModelPrStateProjectionPage.model_validate(
                {
                    "topic": body["topic"],
                    "rows": rows,
                    "truncated": body["truncated"],
                    "cursor": max(
                        (str(row["observed_at"]) for row in rows), default=None
                    ),
                }
            )
        except (KeyError, TypeError, ValidationError):
            return ModelPrStateProjectionRefusal(
                status_code=response.status_code, error=MALFORMED
            )
