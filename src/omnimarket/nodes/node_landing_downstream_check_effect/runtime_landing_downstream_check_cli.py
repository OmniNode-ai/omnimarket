# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Run the downstream check from the landing controller's host: a JSON request on stdin, the fact on stdout.

Exit 0 on a pass, 3 on a break or an unread (the controller holds the head), 2 on a request that does not parse.
"""

from __future__ import annotations

import sys

from pydantic import ValidationError

from omnimarket.models.model_downstream_gate import EnumDownstreamVerdict

from .handlers.handler_landing_downstream_check import HandlerLandingDownstreamCheck
from .models import ModelLandingDownstreamCheckRequest


def main(argv: list[str] | None = None) -> int:
    del argv
    try:
        request = ModelLandingDownstreamCheckRequest.model_validate_json(
            sys.stdin.read()
        )
    except ValidationError as exc:
        sys.stderr.write(f"landing_downstream_check: bad request: {exc}\n")
        return 2
    fact = HandlerLandingDownstreamCheck().handle(request)
    sys.stdout.write(fact.model_dump_json() + "\n")
    sys.stderr.write(fact.line() + "\n")
    return 0 if fact.verdict is EnumDownstreamVerdict.PASS else 3
