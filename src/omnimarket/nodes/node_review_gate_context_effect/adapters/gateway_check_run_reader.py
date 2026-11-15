# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Read a head's check runs through the GitHub GATEWAY node over the bus.

The request is the gateway's ``commit_check_runs`` read: the repository, the head and the check name. The answer is
validated as the gateway's typed result before it maps to head check runs, and a refused or unreadable terminal is a
typed port error. The reader never talks to GitHub itself.
"""

from __future__ import annotations

from collections.abc import Callable

from pydantic import JsonValue, ValidationError

from omnimarket.models.model_github_gateway_io import (
    EnumGithubGatewayOperation,
    ModelCommitCheckRunsResult,
    ModelGithubGatewayRequest,
)
from omnimarket.nodes.node_review_gate_context_effect.models.model_review_context_post import (
    ModelHeadCheckRun,
)
from omnimarket.nodes.node_review_gate_context_effect.overlay import (
    ModelReviewContextBusRoute,
)
from omnimarket.nodes.node_review_gate_context_effect.protocols.protocol_head_check_runs import (
    ReviewContextPortError,
)

#: One gateway request in, the terminal's (failed, result) out.
GatewayCall = Callable[[ModelGithubGatewayRequest], tuple[bool, dict[str, JsonValue]]]


class GatewayCheckRunReader:
    """ProtocolHeadCheckRunReader that reads through the gateway node."""

    def __init__(self, route: ModelReviewContextBusRoute, call: GatewayCall) -> None:
        self._route = route
        self._call = call

    def read(
        self, *, repository: str, head_sha: str, check_name: str
    ) -> tuple[ModelHeadCheckRun, ...]:
        request = ModelGithubGatewayRequest(
            operation=EnumGithubGatewayOperation.COMMIT_CHECK_RUNS,
            repo=repository,
            head_sha=head_sha,
            check_name=check_name,
        )
        failed, result = self._call(request)
        if failed:
            reason = str(result.get("reason", "unknown"))
            raise ReviewContextPortError(f"gateway failed: {reason}")
        try:
            answer = ModelCommitCheckRunsResult.model_validate(result)
        except ValidationError as exc:
            raise ReviewContextPortError("gateway answer does not validate") from exc
        return tuple(
            ModelHeadCheckRun(
                check_run_id=run.id,
                name=run.name,
                head_sha=head_sha,
                status=run.status,
                conclusion=run.conclusion,
            )
            for run in answer.check_runs
        )


__all__ = ["GatewayCall", "GatewayCheckRunReader"]
