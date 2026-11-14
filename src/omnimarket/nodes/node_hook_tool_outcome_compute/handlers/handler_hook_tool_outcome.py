# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
from __future__ import annotations

import json
import re

from omnimarket.nodes.node_hook_tool_outcome_compute.models.enum_tool_outcome import (
    EnumToolOutcome,
)
from omnimarket.nodes.node_hook_tool_outcome_compute.models.model_hook_tool_outcome import (
    ModelHookToolOutcomeRequest,
    ModelHookToolOutcomeResult,
    ModelToolOutcomeRules,
)

__all__ = ["HandlerHookToolOutcome", "ModelHookToolOutcomeRequest"]


class HandlerHookToolOutcome:
    def __init__(self, rules: ModelToolOutcomeRules) -> None:
        self._rules = rules
        self._compiled_rules = [
            (rule, re.compile(rule.pattern, re.MULTILINE))
            for rule in rules.refusal_rules
        ]
        self._exit_code_re = re.compile(rules.exit_code_pattern)

    def handle(
        self, request: ModelHookToolOutcomeRequest
    ) -> ModelHookToolOutcomeResult:
        text = self._build_text(request)
        error_text = request.error or ""

        # 1. REFUSED: first matching rule wins
        for rule, compiled in self._compiled_rules:
            if request.tool_name not in rule.tool_names:
                continue
            match = compiled.search(text)
            if match:
                component = (
                    match.group("component")
                    if match.groupdict().get("component") is not None
                    else rule.component
                )
                return ModelHookToolOutcomeResult(
                    outcome=EnumToolOutcome.REFUSED,
                    refusing_component=component,
                    matched_rule=rule.name,
                    exit_code=request.exit_code,
                    reason=f"refused by {component}",
                )

        # 2. TIMEOUT
        exit_code = self._resolve_exit_code(request, error_text)
        interrupted = request.interrupted
        if interrupted is None and request.tool_response:
            try:
                parsed = json.loads(request.tool_response)
                if isinstance(parsed, dict):
                    interrupted = parsed.get("interrupted")
            except (ValueError, TypeError):
                pass
        if (
            (self._rules.interrupted_is_timeout and interrupted is True)
            or (exit_code is not None and exit_code in self._rules.timeout_exit_codes)
            or any(
                marker in error_text or marker in self._stderr_text(request)
                for marker in self._rules.timeout_markers
            )
        ):
            return ModelHookToolOutcomeResult(
                outcome=EnumToolOutcome.TIMEOUT,
                refusing_component=None,
                matched_rule="timeout",
                exit_code=exit_code,
                reason="timed out",
            )

        # 3. FAILED
        if request.hook_event_name == "PostToolUseFailure" or (
            exit_code is not None and exit_code != 0
        ):
            return ModelHookToolOutcomeResult(
                outcome=EnumToolOutcome.FAILED,
                refusing_component=None,
                matched_rule=None,
                exit_code=exit_code,
                reason="tool call failed",
            )

        # 4. OK
        return ModelHookToolOutcomeResult(
            outcome=EnumToolOutcome.OK,
            refusing_component=None,
            matched_rule=None,
            exit_code=exit_code,
            reason="tool call succeeded",
        )

    def _build_text(self, request: ModelHookToolOutcomeRequest) -> str:
        parts: list[str] = []
        if request.tool_response:
            try:
                parsed = json.loads(request.tool_response)
                if isinstance(parsed, dict):
                    stdout = parsed.get("stdout", "")
                    stderr = parsed.get("stderr", "")
                    if stdout:
                        parts.append(str(stdout))
                    if stderr:
                        parts.append(str(stderr))
                else:
                    parts.append(request.tool_response)
            except (ValueError, TypeError):
                parts.append(request.tool_response)
        if request.error:
            parts.append(request.error)
        return "\n".join(parts)

    def _stderr_text(self, request: ModelHookToolOutcomeRequest) -> str:
        if not request.tool_response:
            return ""
        try:
            parsed = json.loads(request.tool_response)
        except (ValueError, TypeError):
            return ""
        if isinstance(parsed, dict):
            return str(parsed.get("stderr", "") or "")
        return ""

    def _resolve_exit_code(
        self, request: ModelHookToolOutcomeRequest, error_text: str
    ) -> int | None:
        if request.exit_code is not None:
            return request.exit_code
        if error_text:
            match = self._exit_code_re.search(error_text)
            if match:
                return int(match.group(1))
        return None
