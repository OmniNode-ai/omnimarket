# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Pure policy shared by the check node, lane dispatch and controller briefs.

A structured release strategy is refused for an internal consumer. The text leg
also catches release instructions in existing unstructured briefs. It is a
bounded instruction check, not an interpretation of logs or quoted evidence.
"""

from __future__ import annotations

import re
from typing import Literal

from omnimarket.models.model_internal_release_path_guard import (
    ModelInternalReleasePathGuardRequest,
    ModelInternalReleasePathGuardResult,
    ModelInternalReleasePathViolation,
)

_RELEASE = re.compile(
    r"\b(?:(?:cut(?:ting)?|publish(?:ing)?|creat(?:e|ing)|ship(?:ping)?|issu(?:e|ing)|mak(?:e|ing)|wait(?:ing)?\s+for|await(?:ing)?|requires?|needs?)"
    r"\s+(?:(?:a|an|the|new|next|patch|minor|major|package|sibling|upstream)\s+)*release\b"
    r"|release\s+(?:omnibase[-_](?:core|infra|spi)|omnimarket|omniclaude|omniintelligence)\b"
    r"|(?:uv|poetry)\s+publish\b|twine\s+upload\b)",
    re.IGNORECASE,
)
# Negation must govern the proposed action in its own clause, not an unrelated
# earlier failure ("tests did not pass; cut a release" must still be refused).
_NEGATED = re.compile(
    r"\b(?:do\s+not|don't|never|must\s+not|should\s+not|cannot|can't|"
    r"refus\w*|reject\w*|forbid\w*|prohibit\w*|guard\s+against)\b[^.;\n]*$",
    re.IGNORECASE,
)
_CLAUSE_BREAK = re.compile(
    r"[.;:!?]|\b(?:but|then|instead|however|otherwise)\b", re.IGNORECASE
)
_REMEDIATION = (
    "internal-release-path: use the sibling's dev tag or immutable commit pin, "
    "run the consumer integration tests and propose the pin change by PR"
)


class HandlerInternalReleasePathGuard:
    """Canonical compute handler for internal dependency release refusals."""

    @property
    def handler_type(self) -> Literal["NODE_HANDLER"]:
        return "NODE_HANDLER"

    @property
    def handler_category(self) -> Literal["COMPUTE"]:
        return "COMPUTE"

    def handle(
        self, request: ModelInternalReleasePathGuardRequest
    ) -> ModelInternalReleasePathGuardResult:
        violations: list[ModelInternalReleasePathViolation] = []
        if request.consumer_scope == "internal":
            if request.dependency_strategy == "release":
                violations.append(
                    ModelInternalReleasePathViolation(
                        line=0, text="dependency_strategy=release"
                    )
                )
            for number, line in enumerate(request.proposed_actions.splitlines(), 1):
                previous_end = 0
                for match in _RELEASE.finditer(line):
                    prefix = _CLAUSE_BREAK.split(line[previous_end : match.start()])[-1]
                    previous_end = match.end()
                    if _NEGATED.search(prefix):
                        continue
                    violations.append(
                        ModelInternalReleasePathViolation(
                            line=number, text=line.strip()
                        )
                    )
                    break
        return ModelInternalReleasePathGuardResult(
            allowed=not violations,
            exit_code=1 if violations else 0,
            violations=tuple(violations),
            remediation=_REMEDIATION if violations else "",
        )


def check_internal_release_path(
    proposed_actions: str,
) -> ModelInternalReleasePathGuardResult:
    """The shared pure check used by internal lane and controller callers."""
    return HandlerInternalReleasePathGuard().handle(
        ModelInternalReleasePathGuardRequest(proposed_actions=proposed_actions)
    )
