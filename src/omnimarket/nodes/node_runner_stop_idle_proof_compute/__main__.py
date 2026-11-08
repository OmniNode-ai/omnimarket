# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""``python -m omnimarket.nodes.node_runner_stop_idle_proof_compute``: one JSON request on stdin, one JSON proof on stdout."""

from __future__ import annotations

import sys

from omnimarket.nodes.node_runner_stop_idle_proof_compute.handlers import (
    HandlerRunnerStopIdleProof,
)
from omnimarket.nodes.node_runner_stop_idle_proof_compute.models import (
    ModelRunnerStopIdleProofRequest,
)


def main() -> int:
    request = ModelRunnerStopIdleProofRequest.model_validate_json(sys.stdin.read())
    sys.stdout.write(HandlerRunnerStopIdleProof().handle(request).model_dump_json())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
