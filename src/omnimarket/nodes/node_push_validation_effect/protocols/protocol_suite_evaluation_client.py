# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Protocol seam for the fixed-commit suite-evaluation operation (OMN-16524).

Mirrors `protocol_push_validation_client.py`'s discipline: the handler
depends only on this protocol so the acceptance suite drives the pass/fail
outcome with seeded stubs and NO live git/docker. The bounded, containerized
implementation (`SuiteEvaluationGateContainerSubprocess`) lives beside this
protocol.

Deliberately a NARROWER protocol than `ProtocolPushValidationClient` — this
operation never fetches a branch, never installs a hook, never pushes.
`evaluate_commit` is ONE observation call (mirrors "exactly one observation,
never refetch-and-continue" from the push-validation handler), returning
everything the receipt needs from a single I/O boundary.

Error seam: implementations RAISE on infrastructure errors (clone/fetch
failure, unknown commit, toolchain absent, container unreachable) — the
handler lets those propagate to the failure terminal topic. A red suite is
NOT an infrastructure error; it is a domain outcome captured in the returned
`ModelSuiteEvaluationResult` (verdict=fail), same discipline as
`ModelSuiteRun.passed=False` in the sibling protocol.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.nodes.node_push_validation_effect.protocols.protocol_push_validation_client import (
    ModelSuiteRun,
)


class ModelSuiteEvaluationResult(BaseModel):
    """Result of one fixed-commit checkout + suite evaluation."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    evaluated_tree_digest: str = Field(
        ...,
        pattern=r"^[0-9a-f]{40}$",
        description="git tree SHA of the checked-out commit, computed by the "
        "implementation AFTER checkout — never the caller's claim.",
    )
    selector_policy_digest: str = Field(
        ...,
        pattern=r"^[0-9a-f]{64}$",
        description="sha256 hex binding suite_scope + the evaluated commit's "
        "pyproject.toml bytes (when present).",
    )
    suite: ModelSuiteRun = Field(
        ...,
        description="Reused from the push-validation protocol: passed / "
        "log_digest / detail.",
    )


@runtime_checkable
class ProtocolSuiteEvaluationClient(Protocol):
    """Protocol for the R1 fixed-commit suite-evaluation side effects."""

    def evaluate_commit(
        self, repo: str, commit_sha: str, suite_scope: str
    ) -> ModelSuiteEvaluationResult:
        """Check out `commit_sha` (detached) inside the bounded execution
        environment, compute its tree digest and selector-policy digest, run
        `suite_scope`, and digest the complete log.

        Implementations MUST verify `commit_sha` is a known object (RAISE if
        not — an unknown commit is an infrastructure/input error, never a
        domain outcome) and MUST checkout detached at exactly `commit_sha`,
        never "whatever a branch currently is."
        """
        ...

    def read_host_identity(self) -> str:
        """Machine identity readback (e.g. the gate-runner container's
        stable hostname). Always non-empty."""
        ...


__all__ = [
    "ModelSuiteEvaluationResult",
    "ProtocolSuiteEvaluationClient",
]
