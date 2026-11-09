# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Process entry of the agent-prompt gh-read guard: ``python -m`` this module (OMN-23307).

The PreToolUse Agent/Workflow entrypoint (``pre_tool_use_agent_model_guard.sh``) pipes the hook
payload here when it names gh or the GitHub API, and passes the session's omniclaude_mode. This
module reads the payload, takes the texts the contract's ``guard_policy`` names for the tool,
runs :class:`HandlerAgentPromptGhReadGuard` and prints the verdict.

Exit codes: 0 allow (nothing printed), 2 refuse (the block JSON on stdout), 1 an input or policy
it cannot judge (the entrypoint refuses that too, fail-closed).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from omnimarket.nodes.node_agent_prompt_gh_read_guard_effect.enums import (
    EnumAgentPromptGhReadVerdict,
    EnumGuardSessionMode,
)
from omnimarket.nodes.node_agent_prompt_gh_read_guard_effect.handlers.handler_agent_prompt_gh_read_guard import (
    HandlerAgentPromptGhReadGuard,
)
from omnimarket.nodes.node_agent_prompt_gh_read_guard_effect.handlers.handler_contract_policy_source import (
    GuardPolicyError,
    load_policy,
)
from omnimarket.nodes.node_agent_prompt_gh_read_guard_effect.models import (
    ModelAgentPromptGhReadPolicy,
    ModelAgentPromptGhReadRequest,
    ModelAgentPromptSurface,
)

EXIT_ALLOW = 0
EXIT_ERROR = 1
EXIT_REFUSE = 2


def _string(value: object) -> str:
    return value if isinstance(value, str) else ""


def build_request(
    payload: dict[str, object],
    policy: ModelAgentPromptGhReadPolicy,
    mode: EnumGuardSessionMode = EnumGuardSessionMode.FULL,
) -> ModelAgentPromptGhReadRequest:
    """The request for one hook payload: the texts the policy names for its tool."""
    tool_name = _string(payload.get("tool_name"))
    raw_input = payload.get("tool_input")
    tool_input: dict[str, object] = raw_input if isinstance(raw_input, dict) else {}
    surfaces: list[ModelAgentPromptSurface] = []
    for name in policy.judged_fields.get(tool_name, ()):
        text = _string(tool_input.get(name))
        label = f"{tool_name} {name}"
        if not text and name in policy.code_fields:
            path_field = policy.path_fields.get(tool_name, "")
            path = _string(tool_input.get(path_field)) if path_field else ""
            try:
                text = Path(path).read_text(encoding="utf-8") if path else ""
            except (OSError, UnicodeDecodeError):
                text = ""  # an unreadable script is the model guard's refusal, not this one's
            label = f"{tool_name} {path_field}" if text else label
        if text:
            surfaces.append(
                ModelAgentPromptSurface(
                    label=label, text=text, is_code=name in policy.code_fields
                )
            )
    return ModelAgentPromptGhReadRequest(
        tool_name=tool_name or "unknown", surfaces=tuple(surfaces), mode=mode
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Agent prompt gh-read guard (OMN-23307)"
    )
    parser.add_argument(
        "--cwd", default="", help="the caller's working directory (unused)"
    )
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
            f"agent prompt gh-read guard: unreadable hook payload ({exc})\n"
        )
        return EXIT_ERROR
    if not isinstance(payload, dict):
        sys.stderr.write("agent prompt gh-read guard: hook payload is not an object\n")
        return EXIT_ERROR
    try:
        policy = load_policy()
        decision = HandlerAgentPromptGhReadGuard(
            refusal_class=policy.refusal_class
        ).handle(build_request(payload, policy, EnumGuardSessionMode(args.mode)))
    except GuardPolicyError as exc:
        sys.stderr.write(f"agent prompt gh-read guard: no policy ({exc})\n")
        return EXIT_ERROR
    if decision.verdict is EnumAgentPromptGhReadVerdict.REFUSE:
        sys.stdout.write(json.dumps(decision.hook_payload()) + "\n")
        return EXIT_REFUSE
    return EXIT_ALLOW


if __name__ == "__main__":
    sys.exit(main())
