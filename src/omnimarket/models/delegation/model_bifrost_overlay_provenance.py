# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Per-field provenance for the bifrost contract + overlay merge (OMN-18670).

The delegation routing config is a COMMITTED contract
(``src/omnimarket/configs/bifrost_delegation.yaml``) deep-merged at load time
with a site overlay — either the store blob under
``delegation.bifrost.overlay`` or, on a standalone install, the machine-local
file ``~/.omninode/delegation/bifrost_overrides.yaml``. The merge is
field-by-field on ``backend_id``, and until this module existed it left no
record of WHICH of the two supplied any given field.

That absence was the whole of the 2026-09-18 delegation outage. The overlay on
one workstation still pinned ``model_name: Qwen3.6-35B-A3B`` on both
``.201:8000`` rungs, four days after every committed surface had been repointed
to ``Qwen3.8-27B``. The local rung then refused with
``model_attribution_mismatch`` naming the value and the endpoint but not the
file, delegation silently climbed to a cloud model, and three separate lanes
re-derived the resolution path by hand because no repository grep can see a
file in ``$HOME``.

The authority rule this module encodes comes from the config-authority
doctrine (``docs/architecture/tutorials/overlays.md``, which cites
``docs/audits/2026-06-10-runtime-env-overlay-authority-audit.md`` as governing):
on-disk overlay files are **a bootstrap fallback only — used when nothing
higher resolves and only with logged provenance**. A committed contract field
that is ``null`` has not been resolved by a higher authority, so the overlay
filling it is the fallback working as designed. A committed field that carries
a value HAS been resolved, so an overlay writing over it is the overlay acting
outside its documented role. Doctrine does not forbid that override — the
delegation config ADR keeps committed values as "defaults, overridable per
tenant, never the tenant's effective truth" — it forbids it being SILENT.
Hence a record, and a WARN, rather than a refusal.

Related:
    - OMN-18670: this ticket — make an overlay override attributable.
    - OMN-17989: retire the machine-local overlay precedence path entirely.
      That is the structural fix; this module is the interim mitigation that
      makes the mechanism legible while it still exists.
    - OMN-16419: the fail-closed ``model_attribution_mismatch`` guard whose
      message this provenance is threaded into. That guard is not relaxed
      here; it gains a source.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Final

from pydantic import BaseModel, ConfigDict, Field

#: Backend fields the COMMITTED contract owns once it declares a value.
#:
#: ``model_name`` is the attribution-bearing field: it is what the fail-closed
#: OMN-16419 guard reconciles against the endpoint's live ``GET /v1/models``,
#: so a stale overlay value here is what takes the local rung dark. The
#: committed contract declares it (``Qwen3.8-27B`` on both local rungs) while
#: deliberately leaving ``endpoint_url`` null for the overlay to supply, so
#: this set is exactly the boundary between the two authorities as the
#: contract itself draws it.
AUTHORITATIVE_BACKEND_FIELDS: Final[frozenset[str]] = frozenset({"model_name"})


class EnumBifrostFieldSource(StrEnum):
    """Which authority supplied a resolved backend field value."""

    COMMITTED_CONTRACT = "committed_contract"
    OVERLAY = "overlay"


class ModelBifrostFieldProvenance(BaseModel):
    """Provenance for one resolved ``backends[<backend_id>].<field_name>``.

    Carries the source kind, the human-readable reference to the artifact that
    supplied it (a filesystem path or a store key), and — when the overlay
    wrote over a value the committed contract had already declared — the
    committed value that was shadowed.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    backend_id: str = Field(..., min_length=1)
    field_name: str = Field(..., min_length=1)
    value: str | None = Field(
        default=None,
        description="The resolved value, rendered as a string. None when null.",
    )
    source: EnumBifrostFieldSource = Field(
        description="Which authority supplied the resolved value."
    )
    source_ref: str = Field(
        ...,
        min_length=1,
        description=(
            "Human-readable provenance of the supplying authority — a "
            "filesystem path or a store key. Carried verbatim into refusal "
            "messages and WARN lines so a stale overlay is diagnosable "
            "without reproducing the load."
        ),
    )
    shadowed_value: str | None = Field(
        default=None,
        description=(
            "The committed-contract value this overlay field wrote over, when "
            "the committed contract had declared one. None when the committed "
            "field was null/absent (the bootstrap-fallback case) or when the "
            "committed contract itself supplied the value."
        ),
    )
    shadowed_source_ref: str | None = Field(
        default=None,
        description="The committed contract that declared ``shadowed_value``.",
    )

    @property
    def shadows_authoritative_field(self) -> bool:
        """True when an overlay wrote over a committed value the contract owns.

        Deliberately true even when the two values AGREE. A redundant overlay
        pin is not harmless: it is silently identical today and silently stale
        the moment the committed contract is repointed, which is precisely how
        the 2026-09-18 overlay became a four-day-old lie without anybody
        editing it.
        """
        return (
            self.source is EnumBifrostFieldSource.OVERLAY
            and self.field_name in AUTHORITATIVE_BACKEND_FIELDS
            and self.shadowed_value is not None
        )

    def describe(self) -> str:
        """Return the one-line attributable description used in refusals/WARNs.

        Names the field, the value, the artifact that supplied it and the key
        within that artifact — and, for a shadow, the committed value written
        over and the contract that declared it. This string is the whole
        deliverable: a refusal carrying it is diagnosable without a repo grep,
        and the refusal that produced this ticket was not.
        """
        rendered = "null" if self.value is None else repr(self.value)
        base = (
            f"{self.field_name}={rendered} supplied by {self.source.value} "
            f"{self.source_ref} at backends[{self.backend_id}].{self.field_name}"
        )
        if self.shadowed_value is None:
            return base
        agreement = (
            "the same value as"
            if self.shadowed_value == self.value
            else "and SHADOWING"
        )
        return (
            f"{base}, {agreement} {self.shadowed_value!r} declared by the "
            f"tracked contract {self.shadowed_source_ref}"
        )


class ModelBifrostOverlayProvenance(BaseModel):
    """Per-field provenance for one whole contract+overlay merge.

    Produced by the merge itself rather than reconstructed afterwards, so the
    record cannot disagree with the config that was actually resolved.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    contract_source: str = Field(
        ..., min_length=1, description="The committed contract that was read."
    )
    overlay_source: str | None = Field(
        default=None,
        description=(
            "The overlay that was merged — a filesystem path or a store key. "
            "None when no overlay was merged at all, which is the deployed-pod "
            "case and is not a defect."
        ),
    )
    fields: tuple[ModelBifrostFieldProvenance, ...] = Field(
        default=(),
        description="One record per resolved backend field, in contract order.",
    )

    def shadows(self) -> tuple[ModelBifrostFieldProvenance, ...]:
        """Return every overlay write over a committed authoritative field."""
        return tuple(f for f in self.fields if f.shadows_authoritative_field)

    def source_for(
        self, backend_id: str, field_name: str
    ) -> ModelBifrostFieldProvenance | None:
        """Return the provenance record for one field, or None if unrecorded."""
        for field in self.fields:
            if field.backend_id == backend_id and field.field_name == field_name:
                return field
        return None


__all__: list[str] = [
    "AUTHORITATIVE_BACKEND_FIELDS",
    "EnumBifrostFieldSource",
    "ModelBifrostFieldProvenance",
    "ModelBifrostOverlayProvenance",
]
