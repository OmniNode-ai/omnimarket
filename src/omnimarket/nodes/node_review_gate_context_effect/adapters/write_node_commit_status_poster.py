# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Post the review-gate commit status through the GitHub WRITE node over the bus.

The request is the write node's ``commit_status_create`` command: the head, the ``fable-review-gate`` context, the
state (success or failure) and the verdict link, with a stable idempotency key and the route's lane. A commit status
is what the write node's token may post where it cannot create a check run. The request model refuses another
context, another state and a short or empty sha, and the poster turns that into a typed port error; a refused or
unreadable terminal is a typed port error too. The poster never talks to GitHub itself.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from uuid import NAMESPACE_URL, UUID, uuid5

from pydantic import JsonValue, ValidationError

from omnimarket.models.model_repo_write_io import (
    EnumRepoWriteOperation,
    ModelRepoWriteRequest,
)
from omnimarket.nodes.node_review_gate_context_effect.overlay import (
    ModelReviewContextBusRoute,
)
from omnimarket.nodes.node_review_gate_context_effect.protocols.protocol_head_statuses import (
    ReviewContextPortError,
)

_IDEM_RE = re.compile(r"[^A-Za-z0-9._:-]")
#: GitHub holds at most 140 characters of a status description.
_MAX_DESCRIPTION = 140


def _correlation_uuid(correlation_id: str) -> UUID:
    """The write node's UUID correlation id: the id itself when it is one, else one derived from it, the same every time."""
    try:
        return UUID(correlation_id)
    except ValueError:
        return uuid5(NAMESPACE_URL, correlation_id)


#: One write request in, the terminal's (failed, result) out.
WriteCall = Callable[[ModelRepoWriteRequest], tuple[bool, dict[str, JsonValue]]]


class WriteNodeCommitStatusPoster:
    """ProtocolHeadStatusPoster that posts through the write node."""

    def __init__(self, route: ModelReviewContextBusRoute, call: WriteCall) -> None:
        self._route = route
        self._call = call

    @staticmethod
    def _idempotency_key(head_sha: str, state: str, correlation_id: str) -> str:
        key = _IDEM_RE.sub(
            "", f"review-ctx-{head_sha[:12]}-{state}-{correlation_id[:8]}"
        )
        if len(key) < 8:
            key = (key + "-" * (8 - len(key)))[:128]
        return key[:128]

    def post(
        self,
        *,
        correlation_id: str,
        repository: str,
        head_sha: str,
        context: str,
        state: str,
        target_url: str,
        description: str,
    ) -> int:
        try:
            request = ModelRepoWriteRequest.model_validate(
                {
                    "correlation_id": _correlation_uuid(correlation_id),
                    "operation": EnumRepoWriteOperation.COMMIT_STATUS_CREATE,
                    "repo": repository,
                    "idempotency_key": self._idempotency_key(
                        head_sha, state, correlation_id
                    ),
                    "requested_by_lane": self._route.requested_by_lane,
                    "commit_sha": head_sha,
                    "status_context": context,
                    "status_state": state,
                    "status_target_url": target_url,
                    "status_description": description[:_MAX_DESCRIPTION] or None,
                    "dry_run": False,
                }
            )
        except ValidationError as exc:
            raise ReviewContextPortError(
                f"the commit status is refused before any call: {len(exc.errors())} invalid field(s)"
            ) from exc
        failed, result = self._call(request)
        if failed:
            reason = str(result.get("reason", "unknown"))
            raise ReviewContextPortError(f"write node failed: {reason}")
        status_id = result.get("commit_status_id")
        if not isinstance(status_id, int) or isinstance(status_id, bool):
            raise ReviewContextPortError(
                "write node completed without a commit_status_id"
            )
        return status_id


__all__ = ["WriteCall", "WriteNodeCommitStatusPoster"]
