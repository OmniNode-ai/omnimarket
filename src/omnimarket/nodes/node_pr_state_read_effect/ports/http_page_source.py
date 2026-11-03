# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Pages of the ``pr_state`` exposure over ``GET /projection/{topic}``.

This is the only request the read surface makes: it asks the projection API
and nothing else.
"""

import httpx
from pydantic import ValidationError

from omnimarket.models.pr_state.model_pr_state_projection_page import (
    ModelPrStateProjectionPage,
    ModelPrStateProjectionRefusal,
)

UNREACHABLE = "projection_unreachable"
REFUSED = "projection_refused"
MALFORMED = "projection_page_malformed"


class HttpPrStatePageSource:
    def __init__(self, client: httpx.Client, topic: str, timeout_seconds: float):
        self._client = client
        self._topic = topic
        self._timeout_seconds = timeout_seconds

    def fetch(
        self, since: str | None
    ) -> ModelPrStateProjectionPage | ModelPrStateProjectionRefusal:
        params = {} if since is None else {"since": since}
        try:
            response = self._client.get(
                f"/projection/{self._topic}",
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
