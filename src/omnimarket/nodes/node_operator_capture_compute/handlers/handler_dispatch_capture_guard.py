# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerDispatchCaptureGuard: no dispatch from an operator message nobody recorded (OMN-20907).

Pure. The PreToolUse entry hands in the tool being called, whether the call comes from a
subagent (``agent_id``) or a headless run, the newest operator message in the session's
transcript (already reduced to its digest by the effect) and the digests the capture store holds
for that session.

* Any tool but Workflow, Agent or Task passes, and so does a subagent or a headless run: the
  rule is about the main session acting on what the operator said.
* No operator message in the transcript passes (a session that has not been spoken to).
* A message whose digest is captured passes.
* Otherwise the dispatch is refused, and the message is returned so the effect captures it in
  the same act; the retry passes. This is how a queued mid-turn message, which never fires the
  prompt hook, or a session whose prompt hook did not run, is still recorded before work starts.
"""

from __future__ import annotations

from omnimarket.models.operator_capture import (
    EnumGuardVerdict,
    ModelDispatchGuardRequest,
    ModelDispatchGuardVerdict,
)

GUARDED_TOOLS = frozenset({"Workflow", "Agent", "Task"})

REFUSAL = (
    "BLOCKED (operator capture, OMN-20907): this session is dispatching work, but the "
    "operator's newest message has no capture record (a queued mid-turn message or a prompt "
    "hook that did not run). It has been captured now; run the same call again. Operator "
    "ruling 2026-10-10: everything the operator decides, asks, suggests or prefers is recorded "
    "before work starts."
)


class HandlerDispatchCaptureGuard:
    def handle(self, request: ModelDispatchGuardRequest) -> ModelDispatchGuardVerdict:
        if request.tool_name not in GUARDED_TOOLS:
            return ModelDispatchGuardVerdict(verdict=EnumGuardVerdict.ALLOW)
        if (request.agent_id and request.agent_id.strip()) or request.headless:
            return ModelDispatchGuardVerdict(verdict=EnumGuardVerdict.ALLOW)
        if request.latest_operator_text is None or not request.latest_operator_digest:
            return ModelDispatchGuardVerdict(
                verdict=EnumGuardVerdict.ALLOW,
                reason="no operator message in the transcript",
            )
        if request.latest_operator_digest in request.captured_digests:
            return ModelDispatchGuardVerdict(verdict=EnumGuardVerdict.ALLOW)
        return ModelDispatchGuardVerdict(
            verdict=EnumGuardVerdict.REFUSE,
            reason=REFUSAL,
            uncaptured_text=request.latest_operator_text,
        )


__all__ = ["GUARDED_TOOLS", "REFUSAL", "HandlerDispatchCaptureGuard"]
