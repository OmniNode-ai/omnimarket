# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""ModelOccCompanionRequest — the S-req seam: ALL machine-observed facts, up front.

The load-bearing input to the pure OCC-companion COMPUTE node (RSD-1, OMN-14285).
Every fact the COMPUTE needs to render the exact companion is carried here so the
COMPUTE handler does **zero I/O** — it never probes GitHub, clones, or stats the
filesystem. A separate read-EFFECT (``node_occ_state_effect``, RSD-2) populates
this model up front; the COMPUTE consumes it.

Purity is load-bearing: the SAME COMPUTE handler is the OMN-14055 / RSD-5
attestation oracle (the gate re-invokes it and byte-diffs the deterministic
subset). Any field the COMPUTE would otherwise discover mid-I/O (adversarial A4 —
``contract_path.is_file()`` after clone) is instead a request field here, so the
oracle can re-run deterministically.

Non-reproducible observed facts (``run_timestamp``, the probe outputs) are also
INPUTS here — the COMPUTE never calls ``datetime.now()`` or runs a probe. This is
what keeps the plan a pure function of the request.
"""

from __future__ import annotations

import re

from pydantic import BaseModel, ConfigDict, Field, field_validator

_GIT_SHA_RE = re.compile(r"^[0-9a-fA-F]{7,40}$")


class ModelObservedProbe(BaseModel):
    """A single machine-observed ``gh`` probe result (command + captured output).

    Observed by the read/write EFFECT and passed in; the COMPUTE renders these
    verbatim into receipts but never executes a probe itself. These fields are
    the "observed-fact subset" (§4.4) the attestation oracle re-probes rather
    than byte-diffs.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    command: str = Field(..., description="The probe command that was executed.")
    stdout: str = Field(
        ..., description="Captured probe stdout (single compact JSON line)."
    )
    exit_code: int = Field(default=0, description="Probe exit code.")


class ModelOccContractState(BaseModel):
    """The up-front OCC-contract state for one cited ticket (read-EFFECT output).

    Resolves adversarial A4: whether ``contracts/<ticket>.yaml`` already exists,
    is merged, and which ``dod_evidence`` entries + whole-file hash it currently
    carries — all gathered by the read-EFFECT so the COMPUTE decides the
    two-audiences supersession purely (OMN-14233 / T3).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    ticket_id: str = Field(..., description="The OMN-XXXX ticket this state is for.")
    exists: bool = Field(
        default=False, description="Does contracts/<ticket>.yaml already exist?"
    )
    merged: bool = Field(
        default=False,
        description="Is the existing contract already merged to the OCC default branch?",
    )
    existing_entry_ids: tuple[str, ...] = Field(
        default=(),
        description="dod_evidence item ids already present in the merged contract.",
    )
    whole_file_sha256: str = Field(
        default="",
        description="sha256 hex of the current committed contract bytes (empty if absent).",
    )
    raw_contract_text: str = Field(
        default="",
        description="The current committed contract YAML (empty if absent).",
    )


class ModelOccCompanionRequest(BaseModel):
    """All PR + OCC state the COMPUTE needs to render the companion, read up front."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    # Product PR machine-observed facts.
    repo: str = Field(..., description="Product repo slug (owner/repo).")
    pr_number: int = Field(..., description="Product PR number.")
    pr_head_sha: str = Field(..., description="Real product PR head SHA (7-40 hex).")
    pr_title: str = Field(default="", description="Product PR title.")
    pr_body: str = Field(default="", description="Product PR body.")
    pr_state: str = Field(default="open", description="Product PR state.")
    pr_head_ref: str = Field(default="", description="Product PR head branch ref.")

    # Authoring identities (verifier MUST differ from runner — OMN-12791).
    runner: str = Field(
        default="node_occ_companion_compute", description="Receipt runner identity."
    )
    verifier: str = Field(
        default="occ-evidence-source-autobind",
        description="Receipt verifier identity (must differ from runner).",
    )

    # Injected observed facts (COMPUTE never generates these).
    run_timestamp: str = Field(
        ..., description="Injected ISO-8601 authoring timestamp (no datetime.now)."
    )
    product_probe: ModelObservedProbe = Field(
        ..., description="Observed gh probe of the product PR."
    )

    # Up-front OCC state (one entry per cited ticket).
    occ_repo: str = Field(
        default="OmniNode-ai/onex_change_control", description="OCC repo slug."
    )
    occ_contract_states: tuple[ModelOccContractState, ...] = Field(
        default=(), description="Per-cited-ticket OCC contract state."
    )

    # OCC-PR facts — None on the first pass; populated on the second pass and on
    # the attestation oracle re-run (the OCC PR is open by then).
    occ_pr_number: int | None = Field(
        default=None, description="OCC companion PR number (once opened)."
    )
    occ_head_sha: str | None = Field(
        default=None, description="OCC companion branch head SHA (once pushed)."
    )
    occ_probe: ModelObservedProbe | None = Field(
        default=None, description="Observed gh probe of the OCC PR (once opened)."
    )

    # Trivial-infra fast-path inputs (OMN-13776).
    changed_files: tuple[str, ...] = Field(
        default=(), description="Product PR changed file paths."
    )
    diff_total_lines: int = Field(
        default=0, description="Total changed diff lines on the product PR."
    )

    # Honest content-read check (OMN-14619): a downstream check_value derived by
    # the read-EFFECT (``node_occ_state_effect``) from the PR diff — a content
    # read pinned to ``pr_head_sha``, asserting a symbol the PR actually adds,
    # RED-controlled against the base ref (see
    # ``reference_occ_receipt_gate_flow``: "assert a symbol the change
    # introduces, not the file's existence"). ``None`` when the read-EFFECT found
    # no RED-controllable candidate (e.g. a non-Python change) — the COMPUTE
    # falls back to the generic ``gh pr view`` state check in that case, exactly
    # as it did before this field existed.
    downstream_check_value: str | None = Field(
        default=None,
        description="Content-read check_value asserting a PR-added symbol, or "
        "None to fall back to the generic PR-state check.",
    )

    @field_validator("pr_head_sha", "occ_head_sha")
    @classmethod
    def _validate_git_sha(cls, value: str | None) -> str | None:
        if value is None:
            return value
        if not _GIT_SHA_RE.fullmatch(value):
            raise ValueError("commit SHA must be 7-40 hexadecimal characters")
        return value

    @field_validator("occ_contract_states")
    @classmethod
    def _validate_unique_occ_contract_states(
        cls, value: tuple[ModelOccContractState, ...]
    ) -> tuple[ModelOccContractState, ...]:
        seen: set[str] = set()
        duplicates: list[str] = []
        for state in value:
            if state.ticket_id in seen:
                duplicates.append(state.ticket_id)
            seen.add(state.ticket_id)
        if duplicates:
            duplicate_list = ", ".join(sorted(set(duplicates)))
            raise ValueError(
                f"occ_contract_states must not contain duplicate ticket_id values: {duplicate_list}"
            )
        return value


__all__ = [
    "ModelObservedProbe",
    "ModelOccCompanionRequest",
    "ModelOccContractState",
]
