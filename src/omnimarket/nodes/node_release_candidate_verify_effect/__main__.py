# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""JSON adapter: a verify request on stdin, the result on stdout; exit 1 when the install is refused."""

from __future__ import annotations

import sys

from pydantic import ValidationError

from omnimarket.nodes.node_release_candidate_verify_effect.handlers.handler_release_candidate_verify import (
    HandlerReleaseCandidateVerify,
)
from omnimarket.nodes.node_release_candidate_verify_effect.models.model_release_candidate_verify_request import (
    ModelReleaseCandidateVerifyRequest,
)


def main() -> int:
    try:
        request = ModelReleaseCandidateVerifyRequest.model_validate_json(
            sys.stdin.read()
        )
    except ValidationError as exc:
        sys.stderr.write(f"release-candidate-verify: invalid request: {exc}\n")
        return 2
    result = HandlerReleaseCandidateVerify().handle(request)
    sys.stdout.write(result.model_dump_json() + "\n")
    return 0 if result.matches else 1


if __name__ == "__main__":
    sys.exit(main())
