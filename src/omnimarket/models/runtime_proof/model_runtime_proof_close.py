# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The facts and the verdict of the runtime-proof close rule.

A ticket closes on proof that its change runs, never on the merge that delivered it.
The request carries what was read (the merge, the receipts, a docs or plan ticket's
declared proof); the decision says whether the ticket closes and why not when it does not.
This narrows what counts as Done evidence and adds no accepted evidence kind.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict


class EnumRuntimeReceiptKind(StrEnum):
    """The only runtime receipts that prove a change runs."""

    LAB_PASS = "lab_pass"
    DEPLOY_AGENT_INSTANCE = "deploy_agent_instance"
    DOD_VERIFY_PASS = "dod_verify_pass"


class EnumProofTicketKind(StrEnum):
    CODE = "code"
    DOCS = "docs"
    PLAN = "plan"


class EnumProofAction(StrEnum):
    CLOSE = "close"
    HELD_NOT_MERGED = "held-not-merged"
    HELD_MERGED_NOT_PROVEN_RUNNING = "held-merged-not-proven-running"
    HELD_DECLARED_PROOF_MISSING = "held-declared-proof-missing"


class ModelRuntimeReceipt(BaseModel):
    """One runtime receipt: what ran, where, and at which revision.

    ``exercises_running_system`` is read only for a ``dod_verify_pass``: a verifier PASS
    counts as runtime proof only when its checks exercise the running system. A lab-pass
    and a deploy-agent instance receipt are runtime receipts by their kind.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: EnumRuntimeReceiptKind
    passed: bool
    revision: str
    host: str = ""
    exercises_running_system: bool = False


class ModelDeclaredProof(BaseModel):
    """The proof a docs-only or plan ticket's own definition of done names."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    satisfied: bool = False
    evidence: str = ""


class ModelProofCloseRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    ticket_id: str
    ticket_kind: EnumProofTicketKind = EnumProofTicketKind.CODE
    change_merged: bool = False
    change_sha: str = ""
    receipts: tuple[ModelRuntimeReceipt, ...] = ()
    declared_proof: ModelDeclaredProof | None = None
    repo_path: str = ""


class ModelProofCloseDecision(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    allowed: bool
    action: EnumProofAction
    reason: str
    proving_receipt: ModelRuntimeReceipt | None = None
