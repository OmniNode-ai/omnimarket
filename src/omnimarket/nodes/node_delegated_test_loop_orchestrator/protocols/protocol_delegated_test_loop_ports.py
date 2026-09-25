# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The seams the delegated test loop sequences (OMN-19362).

One port per child: the prompt compute (T5), the delegate call, the focused
run effect (T3), the failure digest compute (T4), the control grading compute
(T5), and the loop receipt store. The orchestrator owns the order and every
decision; a port only does its one step. Tests drive the orchestrator with fake
ports; the in-process runner (``scripts/dtl/run_delegated_test_loop.py``) binds
them to the real children.
"""

from __future__ import annotations

from typing import Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict

from omnimarket.nodes.node_delegated_test_loop_orchestrator.models.model_delegated_test_loop import (
    ModelControlVerdict,
    ModelDelegatedTestLoopRequest,
    ModelDelegateReply,
    ModelRunDigest,
)


class LoopReceiptExistsError(RuntimeError):
    """A loop receipt already exists for this correlation id: a rerun is refused."""


class ModelPrompt(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    prompt: str
    response_contract: dict[str, object]


class ModelRunReceipt(BaseModel):
    """The part of a focused-run receipt the loop needs."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    receipt_id: str
    status: Literal["completed", "host_busy", "infra_error"]
    exit_code: int | None
    junit_xml: str
    detail: str = ""


@runtime_checkable
class ProtocolDelegatedTestLoopPorts(Protocol):
    def read_target(self, repo: str, ref: str, path: str) -> str: ...

    def build_prompt(
        self,
        request: ModelDelegatedTestLoopRequest,
        target_excerpt: str,
        previous_test: str,
        last: ModelRunDigest | None,
    ) -> ModelPrompt:
        """RAISE ValueError when the bundle carries a forbidden fragment."""
        ...

    def delegate(self, prompt: ModelPrompt, attempt: int) -> ModelDelegateReply: ...

    def run(
        self,
        request: ModelDelegatedTestLoopRequest,
        ref: str,
        ref_role: Literal["fixed", "prefix", "mutation"],
        attempt: int,
        test_source: str,
    ) -> ModelRunReceipt: ...

    def digest(self, receipt: ModelRunReceipt) -> ModelRunDigest: ...

    def grade(
        self,
        prefix_outcome: str,
        mutation_outcome: str | None,
        mutation_requested: bool,
        prefix_ref_equals_fixed_ref: bool,
    ) -> ModelControlVerdict: ...

    def wait_for_host(self, tries: int) -> None:
        """Back off before retrying a run the host refused as busy."""
        ...

    def claim_loop_receipt(self, loop_run_id: str) -> None:
        """RAISE LoopReceiptExistsError when a receipt for this id exists."""
        ...

    def write_loop_receipt(
        self, loop_run_id: str, payload: dict[str, object]
    ) -> None: ...


__all__ = [
    "LoopReceiptExistsError",
    "ModelPrompt",
    "ModelRunReceipt",
    "ProtocolDelegatedTestLoopPorts",
]
