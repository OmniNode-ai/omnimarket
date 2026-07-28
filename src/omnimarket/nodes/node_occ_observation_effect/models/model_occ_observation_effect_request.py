# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""ModelOccObservationEffectRequest — the OCC observation append-write command (OMN-14888).

``mode`` defaults to ``dry_run`` (fail-safe, same convention as
``node_occ_companion_effect``): a bare invocation renders the deterministic
path + content and reports it WITHOUT cloning, pushing, or opening a PR. The
live trigger sets ``mode="mutate"`` — not wired from any workflow yet (see the
OMN-14888 ticket's Architecture note: going live needs a write-capable credential
path this repo's `GITHUB_TOKEN` ambient token cannot provide).
"""

from __future__ import annotations

from typing import Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.events.occ_observation_record import ModelOccObservationRecord
from omnimarket.events.occ_observation_store import OCC_OBSERVATION_EVIDENCE_TICKET


class ModelOccObservationEffectRequest(BaseModel):
    """Command to append (or dry-run) one durable OCC observation record."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    record: ModelOccObservationRecord = Field(
        ..., description="The raw append-only observation record to persist."
    )
    occ_repo: str = Field(
        default="OmniNode-ai/onex_change_control", description="OCC repo slug."
    )
    runner: str = Field(
        default="node_occ_observation_effect",
        description="Receipt runner identity (must differ from verifier — OMN-12791).",
    )
    verifier: str = Field(
        default="occ-observation-append",
        description="Receipt verifier identity (must differ from runner).",
    )
    mode: Literal["dry_run", "mutate"] = Field(
        default="dry_run",
        description="'dry_run' renders the path+content only; 'mutate' clones, "
        "pushes, and opens/reuses the OCC PR.",
    )
    evidence_ticket: str = Field(
        default=OCC_OBSERVATION_EVIDENCE_TICKET,
        pattern=r"^OMN-\d+$",
        description="The ONE ticket every generated OCC PR binds to. It is "
        "rendered into the title, the body's closing-keyword and Evidence-Ticket "
        "lines, and the commit subject, so validator_occ_merge_eligibility can "
        "both EXTRACT it and prove it BOUND. Constrained to a single OMN token "
        "so the emitted PR can never pull an unintended ticket into gate scope "
        "(OMN-15300; guards against the OMN-15194 / OMN-14658 title-scan "
        "over-demand). It is the observation-store ticket, NOT the triggering "
        "product PR's ticket — see OCC_OBSERVATION_EVIDENCE_TICKET for why "
        "citing the product ticket returns missing_contract (OMN-15323). The "
        "payload builder now emits this field EXPLICITLY rather than relying on "
        "this default, so the choice is a visible seam at the call site.",
    )
    correlation_id: UUID = Field(default_factory=uuid4)


__all__ = ["ModelOccObservationEffectRequest"]
