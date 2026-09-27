# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Replay a recorded loop receipt through the orchestrator (OMN-19362).

The loop receipt records every child result in the order the orchestrator
asked for it. Feeding those results back through the same handler, with no
lab host and no model, must give the same status: that is the replay
determinism the plan's doctrine gate asks for.
"""

from __future__ import annotations

from collections import deque
from typing import Literal

from omnimarket.nodes.node_delegated_test_loop_orchestrator.handlers.handler_delegated_test_loop_orchestrator import (
    HandlerDelegatedTestLoopOrchestrator,
)
from omnimarket.nodes.node_delegated_test_loop_orchestrator.models.model_delegated_test_loop import (
    EnumLoopStatus,
    ModelControlVerdict,
    ModelDelegatedTestLoopRequest,
    ModelDelegateReply,
    ModelGateDigestSeam,
    ModelRunDigest,
)
from omnimarket.nodes.node_delegated_test_loop_orchestrator.protocols.protocol_delegated_test_loop_ports import (
    ModelGateToolRun,
    ModelPrompt,
    ModelRunReceipt,
)


class _ReplayPorts:
    def __init__(self, steps: list[dict[str, object]]) -> None:
        self._replies: deque[ModelDelegateReply] = deque()
        self._runs: deque[ModelRunReceipt] = deque()
        self._digests: dict[str, ModelRunDigest] = {}
        self._grades: deque[ModelControlVerdict] = deque()
        self._gates: deque[ModelGateDigestSeam] = deque()
        for step in steps:
            kind = step.get("kind")
            body = {k: v for k, v in step.items() if k not in {"kind", "attempt"}}
            if kind == "delegate":
                self._replies.append(ModelDelegateReply.model_validate(body))
            elif kind == "host_busy":
                self._runs.append(
                    ModelRunReceipt(
                        receipt_id="", status="host_busy", exit_code=None, junit_xml=""
                    )
                )
            elif kind == "run":
                receipt_id = str(step["receipt_id"])
                status = str(step["status"])
                self._runs.append(
                    ModelRunReceipt(
                        receipt_id=receipt_id,
                        status="completed" if status == "completed" else "infra_error",
                        exit_code=None,
                        junit_xml="",
                        detail=str(step.get("detail", "")),
                        # A gated run is replayed as gated, so the loop asks
                        # for its recorded gate digest (OMN-19527).
                        gate_outputs=(
                            (ModelGateToolRun(path="", gate="recorded"),)
                            if step.get("gated")
                            else ()
                        ),
                    )
                )
                digest = step.get("digest")
                if isinstance(digest, dict):
                    self._digests[receipt_id] = ModelRunDigest.model_validate(digest)
            elif kind == "grade":
                self._grades.append(ModelControlVerdict.model_validate(body))
            elif kind == "gate":
                body.pop("receipt_id", None)
                self._gates.append(ModelGateDigestSeam.model_validate(body))

    def read_target(self, repo: str, ref: str, path: str) -> str:
        return ""

    def build_prompt(
        self,
        request: ModelDelegatedTestLoopRequest,
        target_excerpt: str,
        previous_test: str,
        last: ModelRunDigest | None,
        gate: ModelGateDigestSeam | None = None,
    ) -> ModelPrompt:
        return ModelPrompt(prompt="", response_contract={})

    def delegate(self, prompt: ModelPrompt, attempt: int) -> ModelDelegateReply:
        return self._replies.popleft()

    def run(
        self,
        request: ModelDelegatedTestLoopRequest,
        ref: str,
        ref_role: Literal["fixed", "prefix", "mutation"],
        attempt: int,
        test_source: str,
    ) -> ModelRunReceipt:
        return self._runs.popleft()

    def digest(self, receipt: ModelRunReceipt) -> ModelRunDigest:
        return self._digests[receipt.receipt_id]

    def digest_gates(
        self, receipt: ModelRunReceipt, source: str
    ) -> ModelGateDigestSeam:
        return self._gates.popleft()

    def grade(
        self,
        prefix_outcome: str,
        mutation_outcome: str | None,
        mutation_requested: bool,
        prefix_ref_equals_fixed_ref: bool,
    ) -> ModelControlVerdict:
        return self._grades.popleft()

    def wait_for_host(self, tries: int) -> None:
        return None

    def claim_loop_receipt(self, loop_run_id: str) -> None:
        return None

    def write_loop_receipt(self, loop_run_id: str, payload: dict[str, object]) -> None:
        return None


def replay_loop_receipt(payload: dict[str, object]) -> EnumLoopStatus:
    """The status the orchestrator decides from a receipt's recorded child results."""
    request = ModelDelegatedTestLoopRequest.model_validate(payload["request"])
    steps = payload["steps"]
    if not isinstance(steps, list):
        raise ValueError("a loop receipt's steps must be a list")
    ports = _ReplayPorts([dict(s) for s in steps if isinstance(s, dict)])
    return HandlerDelegatedTestLoopOrchestrator(ports).run(request).status


__all__ = ["replay_loop_receipt"]
