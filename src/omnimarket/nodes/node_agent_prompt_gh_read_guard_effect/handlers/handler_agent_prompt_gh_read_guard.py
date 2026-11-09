# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Refuse an agent prompt that tells a subagent to read GitHub through gh (OMN-23307).

RULING 2026-10-10T18:17:36Z: lanes do not read GitHub; gh is for writes. The real-gh guard
(OMN-22539) refuses a subagent's ``gh`` call when it runs; 92 of the 101 refusals counted in the 24
hours to 2026-11-09T06:46Z came from orchestrator subagents following a prompt that names gh. This
handler judges the prompt before the subagent exists: it runs the lane-text scan
(``node_lane_gh_read_scan_compute``) over the texts of an ``Agent`` or ``Workflow`` dispatch and
refuses with every offending line and the ``onex scm`` verb or local reader that answers it. A
prompt that names the mirror verb, or names gh only to forbid it, is admitted.
"""

from __future__ import annotations

import json

from pydantic import ValidationError

from omnimarket.nodes.node_agent_prompt_gh_read_guard_effect.enums import (
    EnumAgentPromptGhReadVerdict,
    EnumGuardSessionMode,
)
from omnimarket.nodes.node_agent_prompt_gh_read_guard_effect.handlers.handler_contract_policy_source import (
    load_policy,
)
from omnimarket.nodes.node_agent_prompt_gh_read_guard_effect.models import (
    ModelAgentPromptGhReadDecision,
    ModelAgentPromptGhReadRequest,
)
from omnimarket.nodes.node_lane_gh_read_scan_compute.handlers.handler_gh_read_scan import (
    HandlerGhReadScan,
)
from omnimarket.nodes.node_lane_gh_read_scan_compute.models.model_gh_read_scan import (
    ModelGhReadFinding,
    ModelGhReadScanRequest,
    ModelGhReadSurface,
)


def render_refusal(tool_name: str, findings: tuple[ModelGhReadFinding, ...]) -> str:
    lines = [
        f"BLOCKED: this {tool_name} dispatch tells a subagent to call GitHub through gh "
        "(RULING 2026-10-10T18:17:36Z: lanes do not read GitHub; gh is for writes, and a "
        "write goes through onex scm). The subagent's gh call would be refused by the "
        "real-gh guard, so the dispatch is refused here, before the subagent exists. "
        "Reword the prompt to name the mirror:"
    ]
    for finding in findings:
        lines.append(
            f"  {finding.path} line {finding.line}: `{finding.command}` ({finding.shape}) "
            f"-> {finding.mirror}"
        )
    lines.append(
        "A prompt that names gh only to forbid it (never, refused, instead of) passes."
    )
    return "\n".join(lines)


class HandlerAgentPromptGhReadGuard:
    """Judge one Agent or Workflow dispatch: ALLOW, or REFUSE with the mirror verbs."""

    def __init__(
        self,
        scanner: HandlerGhReadScan | None = None,
        refusal_class: str | None = None,
    ) -> None:
        self._scanner = scanner or HandlerGhReadScan()
        self._refusal_class = refusal_class or load_policy().refusal_class

    def handle(
        self, request: ModelAgentPromptGhReadRequest
    ) -> ModelAgentPromptGhReadDecision:
        if request.mode is EnumGuardSessionMode.LITE or not request.surfaces:
            return ModelAgentPromptGhReadDecision(
                verdict=EnumAgentPromptGhReadVerdict.ALLOW
            )
        scan = self._scanner.handle(
            ModelGhReadScanRequest(
                correlation_id=request.correlation_id,
                surfaces=tuple(
                    ModelGhReadSurface(
                        path=s.label,
                        text=s.text,
                        kind="code" if s.is_code else "prose",
                    )
                    for s in request.surfaces
                ),
            )
        )
        if not scan.findings:
            return ModelAgentPromptGhReadDecision(
                verdict=EnumAgentPromptGhReadVerdict.ALLOW
            )
        return ModelAgentPromptGhReadDecision(
            verdict=EnumAgentPromptGhReadVerdict.REFUSE,
            findings=scan.findings,
            reason=render_refusal(request.tool_name, scan.findings),
            refusal_class=self._refusal_class,
        )


def answer_command(
    payload: bytes | str,
    handler: HandlerAgentPromptGhReadGuard,
    completed_topic: str,
    failed_topic: str,
) -> list[tuple[str, bytes]]:
    """The one event a ``check`` command produces: completed with the verdict, or a typed failure."""
    try:
        request = ModelAgentPromptGhReadRequest.model_validate_json(payload)
    except ValidationError as exc:
        try:
            correlation = str(json.loads(payload).get("correlation_id", ""))
        except (ValueError, AttributeError):
            correlation = ""
        failure: dict[str, object] = {
            "correlation_id": correlation,
            "failure": "bad-payload",
            "detail": exc.errors(include_url=False, include_context=False)[0]["msg"],
        }
        return [(failed_topic, json.dumps(failure).encode())]
    decision = handler.handle(request)
    body: dict[str, object] = {
        "correlation_id": request.correlation_id,
        "refused": decision.verdict is EnumAgentPromptGhReadVerdict.REFUSE,
        "findings": [f.model_dump() for f in decision.findings],
        "refusal_class": decision.refusal_class,
    }
    return [(completed_topic, json.dumps(body).encode())]
