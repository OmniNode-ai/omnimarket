# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Read a head's commit statuses through the GitHub GATEWAY node over the bus.

The request is the gateway's ``commit_statuses`` read: the repository and the head. The answer is validated as the
gateway's typed result before it maps to head statuses of the asked context, and a refused or unreadable terminal is
a typed port error. The reader never talks to GitHub itself.
"""

from __future__ import annotations

from collections.abc import Callable

from pydantic import JsonValue, ValidationError

from omnimarket.models.model_github_gateway_io import (
    EnumGithubGatewayOperation,
    ModelCommitStatusesResult,
    ModelGithubGatewayRequest,
)
from omnimarket.models.model_review_context_post import (
    ModelHeadStatus,
)
from omnimarket.nodes.node_review_gate_context_effect.overlay import (
    ModelReviewContextBusRoute,
)
from omnimarket.nodes.node_review_gate_context_effect.protocols.protocol_head_statuses import (
    ReviewContextPortError,
)

#: One gateway request in, the terminal's (failed, result) out.
GatewayCall = Callable[[ModelGithubGatewayRequest], tuple[bool, dict[str, JsonValue]]]


class GatewayCommitStatusReader:
    """ProtocolHeadStatusReader that reads through the gateway node."""

    def __init__(self, route: ModelReviewContextBusRoute, call: GatewayCall) -> None:
        self._route = route
        self._call = call

    def read(
        self, *, repository: str, head_sha: str, context: str
    ) -> tuple[ModelHeadStatus, ...]:
        request = ModelGithubGatewayRequest(
            operation=EnumGithubGatewayOperation.COMMIT_STATUSES,
            repo=repository,
            head_sha=head_sha,
        )
        failed, result = self._call(request)
        if failed:
            reason = str(result.get("reason", "unknown"))
            raise ReviewContextPortError(f"gateway failed: {reason}")
        try:
            answer = ModelCommitStatusesResult.model_validate(result)
        except ValidationError as exc:
            raise ReviewContextPortError("gateway answer does not validate") from exc
        return tuple(
            ModelHeadStatus(
                status_id=status.id,
                context=status.context,
                head_sha=head_sha,
                state=status.state,
            )
            for status in answer.statuses
            if status.context == context
        )


__all__ = ["GatewayCall", "GatewayCommitStatusReader"]
