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

from enum import StrEnum
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field, model_validator

if TYPE_CHECKING:
    from omnimarket.nodes.node_push_validation_effect.models.model_push_validation_request import (
        ModelBundleRef,
    )


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


class EnumBundleFailureMode(StrEnum):
    """Why a bundle failed to materialize — honest, distinguishable causes.

    All four are DOMAIN facts about the request (deterministic and
    caller-attributable), so they produce a completed-topic receipt with
    outcome=bundle_unavailable. Transport faults (S3 unreachable, DNS, 5xx,
    credential failure) are NOT in this enum — implementations RAISE for those
    so they route to the failure terminal topic.
    """

    URL_EXPIRED = "url_expired"
    OVERSIZE = "oversize"
    CHECKSUM_MISMATCH = "checksum_mismatch"
    UNUSABLE_BUNDLE = "unusable_bundle"


class ModelBundleMaterialization(BaseModel):
    """Result of dereferencing + unpacking one transferred bundle (OMN-14979).

    Pre-push commits are not on origin, so the worker fetches a ``git bundle``
    from the transfer bucket and unpacks it into the repo clone under a
    request-scoped ref. ``materialized_ref`` is what the suite then runs
    against — the leg is only real if the suite actually uses it.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    materialized: bool = Field(
        ...,
        description="True only when the bundle was downloaded, verified "
        "(size + sha256 + git bundle verify) and unpacked into a usable ref.",
    )
    failure_mode: EnumBundleFailureMode | None = Field(
        default=None,
        description="Populated exactly when materialized is False; None when "
        "materialized is True. A model validator enforces both directions so "
        "an unexplained failure cannot produce a receipt.",
    )
    materialized_ref: str = Field(
        default="",
        description="The git ref the unpacked bundle was fetched into (e.g. "
        "refs/onex/bundle/<correlation_id>); the suite checks this out "
        "instead of expected_head_sha. Empty when materialization failed.",
    )
    observed_sha256: str = Field(
        default="",
        description="sha256 recomputed over the bytes ACTUALLY received. "
        "Empty when no bytes were read (e.g. deadline already past). The "
        "distinguishing evidence for checksum_mismatch — the receipt records "
        "what was really downloaded, not what was claimed.",
    )
    observed_size_bytes: int = Field(
        default=0,
        ge=0,
        description="Byte count ACTUALLY received. Checked against the cap "
        "independently of the declared size_bytes, so a lying declaration "
        "cannot smuggle an oversize object through.",
    )
    detail: str = Field(
        default="", description="Diagnostic detail on failure (never a URL)."
    )

    @model_validator(mode="after")
    def _failure_mode_agrees_with_materialized(self) -> ModelBundleMaterialization:
        if self.materialized and self.failure_mode is not None:
            raise ValueError(
                "materialized=True requires failure_mode=None — a successful "
                "materialization has no failure cause"
            )
        if not self.materialized and self.failure_mode is None:
            raise ValueError(
                "materialized=False requires an explicit failure_mode — an "
                "unexplained bundle failure must not reach a receipt"
            )
        if self.materialized and not self.materialized_ref.strip():
            raise ValueError(
                "materialized=True requires a non-empty materialized_ref — "
                "the suite has nothing to check out otherwise"
            )
        return self


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

    def materialize_bundle(
        self,
        repo: str,
        branch: str,
        bundle: ModelBundleRef,
        correlation_id: str,
    ) -> ModelBundleMaterialization:
        """Dereference + unpack one transferred bundle into the repo workroot.

        Implementations MUST, in this order: refuse a past ``expires_at``
        before any network call; download via a self-minted presigned GET
        (never a URL taken from the request); bound the read at
        MAX_BUNDLE_BYTES; recompute sha256 over the received bytes; and only
        then unpack into a request-scoped ref.

        The bundle MUST carry exactly ``refs/heads/<branch>``; it is
        unpacked to ``refs/onex/bundle/<correlation_id>`` so concurrent
        requests on one branch cannot overwrite each other.

        Returns a typed failure for the four domain causes; RAISES for
        transport faults (unreachable S3, credential failure, 5xx).
        """
        ...

    def run_suite(
        self,
        repo: str,
        branch: str,
        expected_head_sha: str,
        source_ref: str | None = None,
    ) -> ModelSuiteRun:
        """Run the governed suite; digest the complete log.

        ``source_ref`` is the bundle-materialized ref (OMN-14979). When None
        the suite runs at ``expected_head_sha`` — byte-identical to the
        pre-bundle behavior. When set, the suite MUST run against that ref:
        a bundle leg whose suite still ran the origin commit would be
        theater.
        """
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
    "EnumBundleFailureMode",
    "ModelBranchObservation",
    "ModelBundleMaterialization",
    "ModelHookInstallation",
    "ModelPushResult",
    "ModelSuiteRun",
    "ProtocolPushValidationClient",
]
