# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Shared OCC auto-authoring observation seam (OMN-14393, report-only phase).

Cross-node types for the report-only OCC-companion auto-authoring window:

  * ``OCC_MACHINE_MINTED_LABEL`` / :func:`is_machine_minted` — the *marker seam*.
    ``OccCompanionEmitter`` (legacy) and ``node_occ_companion_effect`` (RSD-3)
    author on the SAME ``auto/…-occ-autobind`` branch prefix, so branch alone
    cannot decide who minted a companion. The write-EFFECT applies this label at
    author time (mutate mode only) so ``minted_by_node`` is decidable — without
    it the window counter cannot distinguish machine-minted from emitter-minted.

  * :class:`ModelOccAutoauthorObservation` — one typed observation record emitted
    by the report-only attestation-observe pass (``node_occ_attestation_observe``)
    and aggregated by the window counter (``node_occ_autoauthor_window``). It is
    the durable evidence unit for the future fail-closed flip (OMN-14393 §4).

These live under ``omnimarket.events`` so neither node reaches into the other's
private model package (repo rule: promote shared types).

REPORT-ONLY / DEFAULT-OFF scope: nothing here blocks a PR, flips a gate
fail-closed, or retires ``OccCompanionEmitter``. It only *observes*.
"""

from __future__ import annotations

from collections.abc import Iterable

from pydantic import BaseModel, ConfigDict, Field

# The distinguishable node-minted marker (design §"Marker-seam gap"). Applied by
# the write-EFFECT (node_occ_companion_effect) at author time, mutate mode only.
OCC_MACHINE_MINTED_LABEL = "occ:machine-minted"

# OMN-16071. The OCC repo runs the OMN-15731 label-gated CI pilot: on a
# dev-targeting PR the heavy `pre-commit` job runs ONLY when `ci:ready` is
# present, and CI Summary then fails closed unless `pre-commit` SUCCEEDED
# (skipped and cancelled both block). A machine-minted companion therefore
# cannot merge -- ever -- until some human hand-applies this label, which is
# exactly how OCC#6540 and its peers #6533/#6529/#6515 stalled.
#
# This is NOT a review gate being auto-satisfied: `ci:ready` only decides
# whether the wave RUNS. Every gate still has to pass on its merits afterwards,
# and CI Summary's strict block is untouched. Applying it at author time simply
# stops the writer from minting PRs that are structurally unmergeable.
OCC_CI_READY_LABEL = "ci:ready"

# Applied together at author time (mutate mode only): the provenance marker and
# the label that lets the companion's CI wave actually fire.
OCC_AUTHOR_TIME_LABELS = (OCC_MACHINE_MINTED_LABEL, OCC_CI_READY_LABEL)


def is_machine_minted(labels: Iterable[str]) -> bool:
    """Pure: True iff the OCC PR carries the machine-minted marker label.

    The single decision point for ``minted_by_node``. Branch prefix is NOT a
    usable discriminator (emitter and node share ``auto/…-occ-autobind``), so the
    label is the authoritative marker.
    """
    return OCC_MACHINE_MINTED_LABEL in set(labels)


class ModelOccAutoauthorObservation(BaseModel):
    """One report-only observation of an OCC companion on a real product PR.

    The evidence unit the N-window aggregates. ``is_clean`` is the per-PR
    acceptance predicate for the future flip: a machine-minted companion whose
    bytes reproduce the canonical compute plan AND whose product PR passes
    occ-preflight.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    product_repo: str = Field(..., description="Product repo slug (owner/repo).")
    product_pr_number: int = Field(..., description="Product PR number observed.")
    occ_pr_number: int | None = Field(
        default=None,
        description="OCC companion PR resolved from the product PR's Evidence-Source (None if unstamped).",
    )
    minted_by_node: bool = Field(
        default=False,
        description="True iff the OCC PR carries OCC_MACHINE_MINTED_LABEL (node-minted, not emitter-minted).",
    )
    attestation_match: bool = Field(
        default=False,
        description="True iff the on-PR companion is byte-reproducible from compute_companion_plan.",
    )
    occ_preflight_eligible: bool = Field(
        default=False,
        description="True iff the product PR's occ-preflight eligibility check concluded success.",
    )
    observed_at: str = Field(
        ..., description="ISO-8601 timestamp the observation was taken (injected)."
    )
    reason: str = Field(
        default="",
        description="Operator-facing explanation, especially when not clean.",
    )

    @property
    def is_clean(self) -> bool:
        """A clean, flip-eligible observation: node-minted + reproducible + eligible."""
        return (
            self.minted_by_node
            and self.attestation_match
            and self.occ_preflight_eligible
        )


__all__ = [
    "OCC_MACHINE_MINTED_LABEL",
    "ModelOccAutoauthorObservation",
    "is_machine_minted",
]
