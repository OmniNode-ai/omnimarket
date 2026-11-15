# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Post a review-verdict check run through the GitHub WRITE node over the bus.

The request is the write node's ``check_run_create`` command: the decision's check name, head, conclusion, title
and summary, with a stable idempotency key and the route's lane. A refused or unreadable terminal is a typed port
error; the poster never talks to GitHub itself.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from uuid import NAMESPACE_URL, UUID, uuid5

from pydantic import JsonValue

from omnimarket.models.model_repo_write_io import (
    EnumRepoWriteOperation,
    ModelCheckRunOutput,
    ModelRepoWriteRequest,
)
from omnimarket.nodes.node_review_gate_context_effect.overlay import (
    ModelReviewContextBusRoute,
)
from omnimarket.nodes.node_review_gate_context_effect.protocols.protocol_head_check_runs import (
    ReviewContextPortError,
)

_IDEM_RE = re.compile(r"[^A-Za-z0-9._:-]")


def _correlation_uuid(correlation_id: str) -> UUID:
    """The write node's UUID correlation id: the id itself when it is one, else one derived from it, the same every time."""
    try:
        return UUID(correlation_id)
    except ValueError:
        return uuid5(NAMESPACE_URL, correlation_id)


#: One write request in, the terminal's (failed, result) out.
WriteCall = Callable[[ModelRepoWriteRequest], tuple[bool, dict[str, JsonValue]]]


class WriteNodeCheckRunPoster:
    """ProtocolHeadCheckRunPoster that posts through the write node."""

    def __init__(self, route: ModelReviewContextBusRoute, call: WriteCall) -> None:
        self._route = route
        self._call = call

    @staticmethod
    def _idempotency_key(head_sha: str, conclusion: str, correlation_id: str) -> str:
        key = _IDEM_RE.sub(
            "", f"review-ctx-{head_sha[:12]}-{conclusion}-{correlation_id[:8]}"
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
        check_name: str,
        conclusion: str,
        title: str,
        summary: str,
    ) -> int:
        request = ModelRepoWriteRequest(
            correlation_id=_correlation_uuid(correlation_id),
            operation=EnumRepoWriteOperation.CHECK_RUN_CREATE,
            repo=repository,
            idempotency_key=self._idempotency_key(head_sha, conclusion, correlation_id),
            requested_by_lane=self._route.requested_by_lane,
            name=check_name,
            head_sha=head_sha,
            status="completed",
            conclusion=conclusion,
            output=ModelCheckRunOutput(title=title, summary=summary),
            dry_run=False,
        )
        failed, result = self._call(request)
        if failed:
            reason = str(result.get("reason", "unknown"))
            raise ReviewContextPortError(f"write node failed: {reason}")
        check_run_id = result.get("check_run_id")
        if not isinstance(check_run_id, int) or isinstance(check_run_id, bool):
            raise ReviewContextPortError("write node completed without a check_run_id")
        return check_run_id


__all__ = ["WriteCall", "WriteNodeCheckRunPoster"]
