# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""What the collector derived from a contract's accepted falsifiers (OMN-20153)."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.enums.enum_dod_acceptance_basis import EnumDodAcceptanceBasis


class ModelDodAcceptanceSummary(BaseModel):
    """Counts of the author's accepted falsifiers and how many became checks."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    declared_falsifier_count: int = Field(
        default=0,
        ge=0,
        description="Accepted criteria that carry a falsifier declaration.",
    )
    runnable_count: int = Field(
        default=0,
        ge=0,
        description="Falsifiers turned into an executable dod_evidence item.",
    )
    unrunnable_labels: tuple[str, ...] = Field(
        default=(),
        description="Labels whose falsifier no machine can run (prose, query).",
    )
    derived_item_ids: tuple[str, ...] = Field(
        default=(),
        description="Evidence ids of the derived items, in label order.",
    )

    @property
    def basis(self) -> EnumDodAcceptanceBasis:
        """The typed answer to what the acceptance evidence was written from."""
        if self.runnable_count > 0:
            return EnumDodAcceptanceBasis.FALSIFIER_CHECKS
        if self.declared_falsifier_count > 0:
            return EnumDodAcceptanceBasis.FALSIFIERS_UNRUNNABLE
        return EnumDodAcceptanceBasis.NO_ACCEPTANCE_CHECKS


__all__ = ["ModelDodAcceptanceSummary"]
