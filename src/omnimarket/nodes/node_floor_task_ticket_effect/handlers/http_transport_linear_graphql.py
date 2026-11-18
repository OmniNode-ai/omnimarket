# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The Linear GraphQL transport of the real-backend ticket store.

Posts one document as JSON with the bare key in the ``Authorization`` header (no
``Bearer`` prefix, matching the Linear auth convention this repo uses) and returns
the decoded answer. A decoded answer that carries GraphQL ``errors`` is returned,
not raised: the store's reader judges it and fails closed. The key never appears in
a repr, string, log line or exception message.
"""

from __future__ import annotations

import json
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any


class LinearGraphqlAnswerError(Exception):
    """The transport's answer is unusable: a non-2xx status or an undecodable body."""


def _default_opener(request: urllib.request.Request, timeout_seconds: float) -> Any:
    return urllib.request.urlopen(request, timeout=timeout_seconds)


@dataclass(frozen=True)
class LinearGraphqlPoster:
    """Post one Linear GraphQL document and return the decoded JSON answer.

    ``opener`` stands in for ``urllib.request.urlopen`` (a test seam); it takes the
    request and a timeout and answers a response whose body reads a UTF-8 string with
    a ``status``.
    """

    url: str
    api_key: str = field(repr=False, compare=False, metadata={"sensitive": True})
    timeout_seconds: float
    opener: Callable[[urllib.request.Request, float], Any] | None = field(
        default=None, repr=False, compare=False
    )

    def post(self, query: str, variables: Mapping[str, object]) -> Mapping[str, Any]:
        payload = json.dumps({"query": query, "variables": dict(variables)}).encode(
            "utf-8"
        )
        request = urllib.request.Request(
            self.url,
            data=payload,
            headers={
                "Content-Type": "application/json",
                # The bare key, no "Bearer" prefix (the established Linear convention).
                "Authorization": self.api_key,
            },
            method="POST",
        )
        opener = self.opener or _default_opener
        response = opener(request, self.timeout_seconds)
        status = int(
            getattr(response, "status", 0) or getattr(response, "code", 0) or 0
        )
        if not 200 <= status < 300:
            raise LinearGraphqlAnswerError(
                f"linear graphql answered with HTTP {status}"
            )
        try:
            body = response.read()
        except Exception:
            raise LinearGraphqlAnswerError("linear graphql answer unreadable") from None
        try:
            answer: Any = json.loads(bytes(body).decode("utf-8"))
        except Exception:
            raise LinearGraphqlAnswerError(
                "linear graphql answer undecodable"
            ) from None
        if not isinstance(answer, Mapping):
            raise LinearGraphqlAnswerError("linear graphql answer is not an object")
        return answer


__all__: list[str] = ["LinearGraphqlAnswerError", "LinearGraphqlPoster"]
