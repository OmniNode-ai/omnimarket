# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The lane guard delegates GitHub reads to the existing bus gateway via onex scm."""

import json
import os
import sys
from pathlib import Path

from .handlers.handler_lane_github_budget import (
    HandlerLaneGithubBudget,
    host_call_snapshot,
)
from .models.model_lane_github_budget import ModelLaneGithubCall, ModelLaneGithubResult


class NodeLaneGithubBudgetEffect:
    contract_path = Path(__file__).with_name("contract.yaml")

    def handle(self, request: ModelLaneGithubCall) -> ModelLaneGithubResult:
        return HandlerLaneGithubBudget().handle(request)


def main() -> int:
    try:
        if len(sys.argv) == 4 and sys.argv[1] == "--host-calls":
            sys.stdout.write(
                json.dumps(host_call_snapshot(sys.argv[2], sys.argv[3] == "1")) + "\n"
            )
            return 0
        policy = json.loads(Path(sys.argv[1]).read_text())
        result = NodeLaneGithubBudgetEffect().handle(
            ModelLaneGithubCall(policy, sys.argv[2], tuple(sys.argv[3:]), os.environ)
        )
    except (OSError, ValueError, TypeError, KeyError, IndexError) as exc:
        sys.stderr.write(
            f"GitHub lane policy unreadable: {type(exc).__name__}; refusing uncounted execution\n"
        )
        return 78
    sys.stdout.write(result.stdout)
    sys.stderr.write(result.stderr)
    return result.code


if __name__ == "__main__":
    raise SystemExit(main())
