# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""ModelOccCompanionPlan — the S-plan seam: the deterministic "what the companion must be".

The output of the pure OCC-companion COMPUTE node (RSD-1, OMN-14285). It is
rendered to two surfaces by the write-EFFECT (RSD-2): the net-new OCC companion
files and the stamped product-PR body. The SAME plan is what the attestation
oracle (RSD-5 / OMN-14055) recomputes and byte-diffs, so it must be a pure
function of :class:`ModelOccCompanionRequest`.

``deterministic_digest`` is the reproducibility fingerprint: a sha256 over the
companion files with the non-reproducible observed-fact lines (``run_timestamp``,
``probe_command``, ``probe_stdout``, ``exit_code``) projected out. Two requests
that differ ONLY in observed facts produce the SAME ``deterministic_digest`` —
this is what lets the oracle re-probe live GitHub and still confirm the
deterministic subset (the property T2 proves).
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.nodes.node_occ_companion_compute.models.enum_companion_file_kind import (
    EnumCompanionFileKind,
)


class ModelCompanionWedge(BaseModel):
    """A self-reported authoring defect, paired with its failure mode + fix.

    Ported from ``scaffold_occ_receipt.detect_wedges`` — the honesty checks that
    make a broken receipt structurally self-reporting instead of silently wrong.
    Pure: derived from the request, never from I/O.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    code: str = Field(..., description="Wedge code, e.g. 'skip_token_present'.")
    failure_mode: str = Field(..., description="What breaks if this ships.")
    alternative: str = Field(..., description="What to do instead.")


class ModelCompanionFile(BaseModel):
    """One net-new OCC companion file the plan emits (byte-exact content)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    path: str = Field(..., description="Path relative to the OCC repo root.")
    content: str = Field(..., description="Byte-exact file content to write.")
    kind: EnumCompanionFileKind = Field(..., description="Companion file kind.")
    ticket_id: str = Field(..., description="The OMN-XXXX ticket this file is for.")
    is_net_new: bool = Field(
        default=True,
        description="Always True — the append-only gate accepts only net-new files "
        "(a fresh contract/receipt or a net-new supersession); a plan never mutates "
        "a merged receipt.",
    )
    contract_sha256: str = Field(
        default="",
        description="Whole-file contract hash bound in this receipt (empty for a contract file).",
    )
    contract_entry_sha256: str = Field(
        default="",
        description="Per-entry hash (OMN-14233) for append/supersede receipts (empty otherwise).",
    )


class ModelOccCompanionPlan(BaseModel):
    """The deterministic companion plan a COMPUTE run emits from a request."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    repo: str = Field(..., description="Product repo slug.")
    pr_number: int = Field(..., description="Product PR number.")
    tickets: tuple[str, ...] = Field(
        default=(), description="The gate-parity cited ticket set (may be empty)."
    )
    branch: str = Field(
        default="", description="Deterministic OCC companion branch name."
    )

    no_op: bool = Field(
        default=False,
        description="True when nothing should be authored (already bound, or no ticket).",
    )
    no_op_reason: str = Field(default="", description="Why the plan is a no-op.")

    fast_path: bool = Field(
        default=False,
        description="True when the trivial-infra fast-path skips the companion (OMN-13776).",
    )
    fast_path_reason: str = Field(default="", description="Fast-path decision reason.")

    companion_files: tuple[ModelCompanionFile, ...] = Field(
        default=(),
        description="Net-new OCC files to write (contract/receipts/supersede).",
    )
    product_body_stamped: str = Field(
        default="",
        description="The product PR body with the canonical Evidence-Source block, "
        "when the OCC PR number is known (else empty).",
    )
    evidence_source_occ_pr: int | None = Field(
        default=None, description="OCC PR number stamped as Evidence-Source (if known)."
    )
    wedges: tuple[ModelCompanionWedge, ...] = Field(
        default=(), description="Self-reported authoring defects (honesty checks)."
    )
    deterministic_digest: str = Field(
        default="",
        description="sha256 over the companion files with observed-fact lines projected "
        "out — the reproducibility fingerprint the attestation oracle byte-diffs.",
    )


__all__ = [
    "ModelCompanionFile",
    "ModelCompanionWedge",
    "ModelOccCompanionPlan",
]
