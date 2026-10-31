# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Process entry of the dev runtime mutation guard: ``python -m`` this module (OMN-21795).

The PreToolUse entrypoint (``pre_tool_use_bash_guards.sh``) pipes the hook payload
here when a Bash command names docker together with a lifecycle verb, and passes the
session's omniclaude_mode. This module reads the payload, runs
:class:`HandlerDevRuntimeMutationGuard` and prints the verdict.

Exit codes: 0 allow (nothing printed), 2 refuse (the block JSON on stdout), 1 an
input or policy it cannot judge (the entrypoint refuses that too, fail-closed).
"""

from __future__ import annotations

import argparse
import json
import os
import sys

from omnimarket.nodes.node_dev_runtime_mutation_guard_effect.enums import (
    EnumDevRuntimeMutationVerdict,
    EnumGuardSessionMode,
)
from omnimarket.nodes.node_dev_runtime_mutation_guard_effect.handlers.handler_contract_policy_source import (
    GuardPolicyError,
)
from omnimarket.nodes.node_dev_runtime_mutation_guard_effect.handlers.handler_dev_runtime_mutation_guard import (
    HandlerDevRuntimeMutationGuard,
)
from omnimarket.nodes.node_dev_runtime_mutation_guard_effect.models import (
    ModelDevRuntimeMutationRequest,
)

EXIT_ALLOW = 0
EXIT_ERROR = 1
EXIT_REFUSE = 2


def _string(value: object) -> str:
    return value if isinstance(value, str) else ""


def build_request(
    payload: dict[str, object],
    fallback_cwd: str,
    mode: EnumGuardSessionMode = EnumGuardSessionMode.FULL,
) -> ModelDevRuntimeMutationRequest:
    """The request for one hook payload."""
    tool_input = payload.get("tool_input")
    command = _string(tool_input.get("command")) if isinstance(tool_input, dict) else ""
    return ModelDevRuntimeMutationRequest(
        tool_name=_string(payload.get("tool_name")),
        command=command,
        cwd=_string(payload.get("cwd")) or fallback_cwd,
        mode=mode,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Dev runtime mutation guard (OMN-21795)"
    )
    parser.add_argument("--cwd", default="", help="the caller's working directory")
    parser.add_argument(
        "--mode",
        default=EnumGuardSessionMode.FULL.value,
        choices=[mode.value for mode in EnumGuardSessionMode],
        help="omniclaude_mode of the session",
    )
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)
    try:
        payload = json.loads(sys.stdin.read())
    except ValueError as exc:
        sys.stderr.write(
            f"dev runtime mutation guard: unreadable hook payload ({exc})\n"
        )
        return EXIT_ERROR
    if not isinstance(payload, dict):
        sys.stderr.write("dev runtime mutation guard: hook payload is not an object\n")
        return EXIT_ERROR
    request = build_request(
        payload, args.cwd or os.getcwd(), EnumGuardSessionMode(args.mode)
    )
    try:
        decision = HandlerDevRuntimeMutationGuard().handle(request)
    except GuardPolicyError as exc:
        sys.stderr.write(f"dev runtime mutation guard: no policy ({exc})\n")
        return EXIT_ERROR
    if decision.verdict is EnumDevRuntimeMutationVerdict.REFUSE:
        sys.stdout.write(json.dumps(decision.hook_payload()) + "\n")
        return EXIT_REFUSE
    return EXIT_ALLOW


if __name__ == "__main__":
    sys.exit(main())
