# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Protocol seam for git/hook/suite/push operations used by HandlerPushValidationEffect.

Mirrors ``node_ticket_work.protocols.protocol_git_client``: the handler depends
only on this protocol so the OMN-14920 acceptance suite drives every outcome
path (stale_head / already_pushed / suite_failed / refused / push_failed /
pushed) with seeded stubs and NO live git. The subprocess-backed implementation
(``GitPushValidationSubprocess``) lives beside this protocol.

Error seam (load-bearing, contract text #2): implementations RAISE on
infrastructure errors — clone/fetch failure, toolchain absent, workroot env
unset. The handler lets those propagate so the runtime routes them to the
FAILURE terminal topic (``onex.evt.omnimarket.push-validation-failed.v1``).
Receipts on the COMPLETED topic are reserved for honest domain outcomes.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field


class ModelBranchObservation(BaseModel):
    """Result of one fetch + head observation for (repo, branch).

    Captured by a SINGLE observation call — the handler never refetches after a
    stale-head divergence (expected_head_sha is FAIL-CLOSED, contract text #1).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    observed_head_sha: str = Field(
        ...,
        min_length=1,
        description="The fetched live head of `branch` — compared FAIL-CLOSED "
        "against request.expected_head_sha; any divergence is stale_head.",
    )
    remote_head_sha: str | None = Field(
        default=None,
        description="Push-destination refs/heads/<branch> readback "
        "(`git ls-remote`); None when the destination ref does not exist.",
    )
    remote_contains_expected: bool = Field(
        default=False,
        description="True when the push destination already has "
        "expected_head_sha as pushed state (head equals it or is a "
        "descendant) — the idempotent already_pushed redelivery path.",
    )


class ModelHookInstallation(BaseModel):
    """Result of installing + reading back the governed pre-push hook."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    installed: bool = Field(
        ..., description="True only when the pre-push hook is verifiably installed."
    )
    hook_id_readback: str = Field(
        default="",
        description="Digest of the INSTALLED pre-push hook content, read back "
        "from the working clone; empty when installation/readback failed. "
        "Captured BEFORE any push attempt — an unhooked clone never pushes.",
    )
    detail: str = Field(default="", description="Diagnostic detail on failure.")


class ModelSuiteRun(BaseModel):
    """Result of one governed-suite run."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    passed: bool = Field(..., description="True only when the suite exited green.")
    log_digest: str = Field(
        ...,
        min_length=1,
        description="sha256 hex of the COMPLETE suite log — a run suite always "
        "has a complete log digest (receipt invariant).",
    )
    detail: str = Field(
        default="", description="Failure summary (e.g. pytest tail) when red."
    )


class ModelPushResult(BaseModel):
    """Result of one push attempt."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    exit_code: int = Field(..., description="git push process exit code.")
    remote_sha_readback: str | None = Field(
        default=None,
        description="`git ls-remote origin refs/heads/<branch>` AFTER the push "
        "attempt; None when unreachable.",
    )
    detail: str = Field(default="", description="stderr tail on failure.")


@runtime_checkable
class ProtocolPushValidationClient(Protocol):
    """Protocol for the push-validation side effects.

    Implementations inject the real subprocess-backed client; tests inject
    seeded stubs. All methods raise on infrastructure errors (routed to the
    failure terminal topic); they return typed results for domain outcomes.
    """

    def observe_branch(
        self, repo: str, branch: str, expected_head_sha: str
    ) -> ModelBranchObservation:
        """Fetch once and observe the live branch head + push-destination state."""
        ...

    def install_hooks(self, repo: str, branch: str) -> ModelHookInstallation:
        """Install the governed pre-push hook and read back its content digest."""
        ...

    def run_suite(
        self, repo: str, branch: str, expected_head_sha: str
    ) -> ModelSuiteRun:
        """Run the governed suite at expected_head_sha; digest the complete log."""
        ...

    def push_branch(
        self, repo: str, branch: str, expected_head_sha: str
    ) -> ModelPushResult:
        """Push exactly expected_head_sha to the destination branch ref."""
        ...

    def read_host_identity(self) -> str:
        """Machine identity readback (e.g. hostname). Always non-empty."""
        ...

    def read_credential_identity(self) -> str:
        """Mechanism-prefixed pushing-credential readback (e.g. 'gh:<login>')."""
        ...


__all__ = [
    "ModelBranchObservation",
    "ModelHookInstallation",
    "ModelPushResult",
    "ModelSuiteRun",
    "ProtocolPushValidationClient",
]
