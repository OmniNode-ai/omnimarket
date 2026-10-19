# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerLaneBriefSection: the `lane_brief_section` profile's factors -> a brief's section 3a (OMN-21118, TA.17).

Pure and definition-B: ``handle(ModelLaneBriefSectionRequest) -> ModelLaneBriefSectionResult``,
no event envelope. The layout is the lane-brief standard's section 3a: ten numbered parts in
a fixed order, each reader's output fenced verbatim. The three required factors
(`LANE_RULES`, `OPERATOR_RULINGS`, `OPEN_WORK`) arrive as source results; their entries are
placed by `source_ref` slot and their text is never edited here. A required factor that did
not resolve, a missing slot, or a cited ruling stamp that no `OPERATOR_RULINGS` entry backs
refuses with `REQUIRED_FACTOR_MISSING` and renders nothing.
"""

from __future__ import annotations

import hashlib

from omnibase_core.enums.enum_context_factor import EnumContextFactor
from omnibase_core.enums.enum_context_pack_failure import EnumContextPackFailure
from omnibase_core.enums.enum_context_source_status import EnumContextSourceStatus
from omnibase_core.models.context.model_context_source_entry import (
    ModelContextSourceEntry,
)
from omnibase_core.models.context.model_context_source_result import (
    ModelContextSourceResult,
)

from omnimarket.models.lane_brief_section import (
    SLOT_CLAIM_TEMPLATE,
    SLOT_DELEGATION,
    SLOT_HOLDS,
    SLOT_INBOX,
    SLOT_LAB,
    SLOT_LANDING_RULES,
    SLOT_LANDING_RULING,
    SLOT_PREAMBLE,
    SLOT_STANDING_RULINGS,
    EnumLaneBriefSectionStatus,
    ModelLaneBriefSectionRequest,
    ModelLaneBriefSectionResult,
)

_FENCE = "```"
_F = EnumContextFactor
#: Slots that hold exactly one entry, and the one slot that holds one or more.
_SINGLETONS: dict[EnumContextFactor, tuple[str, ...]] = {
    _F.LANE_RULES: (SLOT_PREAMBLE, SLOT_CLAIM_TEMPLATE, SLOT_DELEGATION, SLOT_LAB),
    _F.OPERATOR_RULINGS: (SLOT_LANDING_RULING, SLOT_STANDING_RULINGS),
    _F.OPEN_WORK: (SLOT_INBOX, SLOT_HOLDS),
}
_MULTIPLE: dict[EnumContextFactor, tuple[str, ...]] = {
    _F.LANE_RULES: (SLOT_LANDING_RULES,),
}


def _fenced(body: str) -> str:
    if body and not body.endswith("\n"):
        body += "\n"
    return f"{_FENCE}text\n{body}{_FENCE}\n"


def _slots(
    result: ModelContextSourceResult | None,
) -> dict[str, list[ModelContextSourceEntry]]:
    slots: dict[str, list[ModelContextSourceEntry]] = {}
    if result is None or result.status is not EnumContextSourceStatus.RESOLVED:
        return slots
    for entry in result.entries:
        if entry.status is EnumContextSourceStatus.RESOLVED and entry.source_ref:
            slots.setdefault(entry.source_ref, []).append(entry)
    return slots


def _refuse(
    missing_factors: list[EnumContextFactor],
    missing_slots: list[str],
    unbacked: list[str],
) -> ModelLaneBriefSectionResult:
    return ModelLaneBriefSectionResult(
        status=EnumLaneBriefSectionStatus.REFUSED,
        failure_class=EnumContextPackFailure.REQUIRED_FACTOR_MISSING,
        missing_factors=tuple(missing_factors),
        missing_slots=tuple(missing_slots),
        unbacked_ruling_stamps=tuple(unbacked),
    )


class HandlerLaneBriefSection:
    """Render section 3a from the `lane_brief_section` profile's required factors."""

    def handle(
        self, request: ModelLaneBriefSectionRequest
    ) -> ModelLaneBriefSectionResult:
        factors = {
            _F.LANE_RULES: request.lane_rules,
            _F.OPERATOR_RULINGS: request.operator_rulings,
            _F.OPEN_WORK: request.open_work,
        }
        missing_factors: list[EnumContextFactor] = []
        missing_slots: list[str] = []
        placed: dict[EnumContextFactor, dict[str, list[ModelContextSourceEntry]]] = {}
        for factor, result in factors.items():
            slots = _slots(result)
            placed[factor] = slots
            if not slots:
                missing_factors.append(factor)
                continue
            for slot in _SINGLETONS[factor]:
                count = len(slots.get(slot, ()))
                if count == 0:
                    missing_slots.append(f"{factor.value}:{slot}")
                elif count > 1:
                    missing_slots.append(f"{factor.value}:{slot}:ambiguous")
            for slot in _MULTIPLE.get(factor, ()):
                if not slots.get(slot):
                    missing_slots.append(f"{factor.value}:{slot}")

        ruling_text = [
            entry.text
            for entries in placed[_F.OPERATOR_RULINGS].values()
            for entry in entries
        ]
        landing_prefix = (
            f"{request.landing_ruling} | RULING | lane={request.landing_ruling_lane} |"
        )
        unbacked: list[str] = []
        if placed[_F.OPERATOR_RULINGS] and not any(
            landing_prefix in text for text in ruling_text
        ):
            unbacked.append(request.landing_ruling)
        for stamp in request.cited_ruling_stamps:
            if (
                placed[_F.OPERATOR_RULINGS]
                and stamp not in unbacked
                and not any(f"{stamp} | RULING |" in text for text in ruling_text)
            ):
                unbacked.append(stamp)
        if missing_factors or missing_slots or unbacked:
            return _refuse(missing_factors, missing_slots, unbacked)

        lane_rules = placed[_F.LANE_RULES]
        rulings = placed[_F.OPERATOR_RULINGS]
        work = placed[_F.OPEN_WORK]
        out: list[str] = [
            "## 3a. Constraints (pasted, never paraphrased)",
            "",
            lane_rules[SLOT_PREAMBLE][0].text,
            "",
            f"### 3a.1 Inbox (inbox.sh {request.lane}, rc={request.inbox_rc})",
            "",
            _fenced(work[SLOT_INBOX][0].text),
            f"### 3a.2 Holds in force (catchup.sh section 2, CATCHUP_T0={request.ledger_t0}, "
            f"hours={request.hours}, rc={request.holds_rc})",
            "",
            _fenced(work[SLOT_HOLDS][0].text),
            f"### 3a.3 Landing (RULING {request.landing_ruling} lane={request.landing_ruling_lane}; "
            f"target={request.landing_lane})",
            "",
            _fenced(rulings[SLOT_LANDING_RULING][0].text),
            *(entry.text for entry in lane_rules[SLOT_LANDING_RULES]),
            "",
            f"### 3a.4 Standing rulings (step 1.5 grep, newest {request.rulings_tail})",
            "",
            _fenced(rulings[SLOT_STANDING_RULINGS][0].text),
            "### 3a.5 Model and effort",
            "",
            f"model={request.model} effort={request.effort}",
        ]
        if request.route_line:
            out.append(request.route_line)
        out += [
            "",
            "### 3a.6 Consent",
            "",
            *request.consent_lines,
            "",
            "### 3a.7 Out of scope",
            "",
            f"OUT OF SCOPE: {request.out_of_scope.strip()}",
            "",
            "### 3a.8 CLAIM template",
            "",
            "Append before the first write, through /omni:ledger-write, filling the <...> cells:",
            "",
            f"{_FENCE}bash",
            lane_rules[SLOT_CLAIM_TEMPLATE][0].text + _FENCE,
            "",
            lane_rules[SLOT_DELEGATION][0].text,
            "",
            lane_rules[SLOT_LAB][0].text,
            "",
        ]
        section = "\n".join(out)
        return ModelLaneBriefSectionResult(
            status=EnumLaneBriefSectionStatus.RENDERED,
            section_3a=section,
            section_sha256=hashlib.sha256(section.encode("utf-8")).hexdigest(),
        )


__all__ = ["HandlerLaneBriefSection"]
