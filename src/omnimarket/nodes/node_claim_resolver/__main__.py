# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""CLI for node_claim_resolver.

Reads a ModelClaimResolutionRequest JSON document from stdin and writes a
ModelClaimResolutionResponse JSON document to stdout.
"""

from __future__ import annotations

import json
import sys

from pydantic import ValidationError

from omnimarket.nodes.node_claim_resolver.handlers.handler_claim_resolver import (
    HandlerClaimResolver,
)
from omnimarket.nodes.node_claim_resolver.models import ModelClaimResolutionRequest


def main() -> int:
    try:
        payload = json.loads(sys.stdin.read() or "{}")
        request = ModelClaimResolutionRequest.model_validate(payload)
    except (json.JSONDecodeError, ValidationError) as exc:
        sys.stderr.write(f"CLAIM_RESOLVER_BAD_REQUEST:{exc}\n")
        return 1

    try:
        response = HandlerClaimResolver().verify(request)
    except Exception as exc:
        sys.stderr.write(f"CLAIM_RESOLVER_RUNTIME_ERROR:{exc}\n")
        return 1
    sys.stdout.write(response.model_dump_json(indent=2) + "\n")
    return 2 if response.mismatches else 0


if __name__ == "__main__":
    sys.exit(main())
