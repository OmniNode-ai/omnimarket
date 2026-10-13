# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Compare a ticket contract's pinned criteria with the live ticket (OMN-20858).

A contract's ``ac_bindings`` pin each binding to the hash of the criterion text it
was accepted against. ``onex_change_control`` refuses a binding whose pin no longer
matches the live text (``ac_binding_stale_hash``, ``validation/ac_binding_acceptance.py``
``_stale_pins`` at lines 696-730, and ``ac_requirements_stale_pin``, ``serialization/
ac_requirements.py`` lines 397-411), so the closer can hold a ticket whose criterion
changed after acceptance (OMN-18330, omnibase_infra#3497). Until this module dod_verify
never read the live ticket, so verify and the closer disagreed.

The hash is the change-control hash, not a second definition: ``criterion_hash`` and
``criteria_by_label`` here are :mod:`omnimarket.occ_contract_pin`, a byte-for-byte port
of ``onex_change_control`` ``src/onex_change_control/validation/ac_criteria.py`` at
``ab4be01b`` (unchanged on its ``dev`` since). Whitespace runs collapse to one space and
nothing else is normalised, so a wording change, a negation or a changed threshold is a
different hash and a re-flowed paragraph is not.

The rules reproduce the change-control gate's, and name where they diverge:

* A binding is stale when its pinned hash is not the live text's hash. A label already
  pinned to the live text by an independently accepted record is re-accepted, and the
  older record beside it (which an append-only contract cannot remove) is not reported
  (``_labels_pinned_to_current_text``). A retired ``(item, label)`` pair is not a pin.
* A record carrying no hash is not comparable and is ignored, where the change-control
  gate reads an absent pin as stale: dod_verify must keep verifying the legacy contracts
  it verified before, so only a pin that exists can drift.
* Two live criteria that resolve to one label with different text are ambiguous and
  refuse. This is stricter than ``ac_requirements_duplicate_label``, where a pin equal to
  the hash of the text the reader resolved arbitrates: here a verdict that rests on one of
  two sentences under one label would not say which, so the ticket must make the label
  unique. Duplicates count only within the reading pass that resolved the label, so a
  verdict line written below the criteria block (the sanctioned place) is not one.
* A label the contract claims and the ticket lacks is deleted; a labelled criterion the
  ticket carries and the contract never claimed is added. Both are reported, never
  silently ignored.

Nothing here reads Linear. The reader is injected as :class:`ProtocolDodTicketCriteriaReader`
and the check engages only for a contract that records criteria at all.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from typing import Any, Protocol, runtime_checkable

from omnimarket.enums.enum_criteria_drift_kind import EnumCriteriaDriftKind
from omnimarket.nodes.node_dod_verify.models.model_criteria_drift import (
    ModelCriteriaAmendment,
    ModelCriteriaAmendmentEntry,
    ModelCriteriaCheck,
    ModelCriterionDrift,
)
from omnimarket.nodes.node_dod_verify.models.model_criteria_pins import (
    ModelCriteriaPins,
    ModelCriterionPin,
)
from omnimarket.nodes.node_dod_verify.models.model_dod_verify_state import (
    EnumEvidenceCheckStatus,
    ModelEvidenceCheckResult,
)
from omnimarket.nodes.node_dod_verify.models.model_ticket_criteria_read import (
    ModelTicketCriteriaRead,
)
from omnimarket.nodes.node_dod_verify.services.ac_binding_retirements import (
    resolve_retirements,
)
from omnimarket.nodes.node_dod_verify.services.ac_falsifier_checks import (
    _canonical_label as _retirement_label,
)
from omnimarket.nodes.node_dod_verify.services.ac_falsifier_checks import (
    is_accepted_binding,
)
from omnimarket.occ_contract_pin import (
    acceptance_criteria_items,
    canonical_ac_label,
    criteria_by_label,
    criterion_hash,
    item_text,
    normalise_criterion,
)

__all__ = [
    "ProtocolDodTicketCriteriaReader",
    "compare_criteria",
    "criteria_revision",
    "evaluate_criteria",
    "extract_criteria_pins",
    "invalidate_drifted_results",
]


@runtime_checkable
class ProtocolDodTicketCriteriaReader(Protocol):
    """Reads the live ticket body; one read, readable or not."""

    def read(self, ticket_id: str) -> ModelTicketCriteriaRead: ...


def extract_criteria_pins(
    contract: Mapping[str, Any], dod_items: Sequence[Any]
) -> ModelCriteriaPins | None:
    """The criteria a ticket contract recorded, or None when it records none.

    A contract that carries neither an ``ac_bindings`` record nor a ``requirements``
    acceptance id has nothing to drift from: its ``binds_ac`` entries alone claim a
    label but record no revision, so reading the ticket for it would refuse every
    legacy contract the verifier verified before this check.
    """
    retired = resolve_retirements(dod_items).pairs
    labels: list[str] = []
    bindings: list[ModelCriterionPin] = []
    engaged = False

    def note(label: str) -> None:
        if label and label not in labels:
            labels.append(label)

    requirements = contract.get("requirements")
    for requirement in requirements if isinstance(requirements, list) else ():
        acceptance = (
            requirement.get("acceptance") if isinstance(requirement, Mapping) else None
        )
        for criterion in acceptance if isinstance(acceptance, list) else ():
            if isinstance(criterion, Mapping) and isinstance(criterion.get("id"), str):
                engaged = True
                note(canonical_ac_label(criterion["id"]))

    for index, item in enumerate(dod_items):
        if not isinstance(item, Mapping):
            continue
        item_id = str(item.get("id") or f"dod_evidence[{index}]")
        binds = item.get("binds_ac")
        for claimed in binds if isinstance(binds, list) else ():
            if isinstance(claimed, str):
                note(canonical_ac_label(claimed))
        records = item.get("ac_bindings")
        for record in records if isinstance(records, list) else ():
            if not isinstance(record, Mapping) or not isinstance(
                record.get("label"), str
            ):
                continue
            engaged = True
            label = canonical_ac_label(record["label"])
            note(label)
            pinned = str(record.get("criterion_hash") or "").strip()
            if (
                not label
                or not pinned
                or (item_id, _retirement_label(record["label"])) in retired
            ):
                continue
            bindings.append(
                ModelCriterionPin(
                    item_id=item_id,
                    label=label,
                    criterion_hash=pinned,
                    accepted=is_accepted_binding(record),
                )
            )
    if not engaged:
        return None
    return ModelCriteriaPins(bindings=tuple(bindings), known_labels=tuple(labels))


def _variants_by_label(description: str) -> dict[str, list[str]]:
    """Every distinct normalised text per label, in reading order."""
    variants: dict[str, list[str]] = {}

    def add(text: str) -> None:
        label = canonical_ac_label(text) if text else ""
        if not label:
            return
        normalised = normalise_criterion(text)
        bucket = variants.setdefault(label, [])
        if normalised not in bucket:
            bucket.append(normalised)

    # The same two passes as ``criteria_by_label``: the criteria sections first,
    # then any labelled line they missed, for a label they did not resolve.
    for item in acceptance_criteria_items(description):
        add(item)
    in_sections = set(variants)
    for raw in description.splitlines():
        text = item_text(raw)
        if text and canonical_ac_label(text) not in in_sections:
            add(text)
    return variants


def criteria_revision(description: str) -> str:
    """The revision of a ticket's labelled criteria: a digest over every label and hash.

    Only the criteria feed it, so an edit to anything else in the body leaves it
    unchanged, and an edit to any criterion (or a second text under one label)
    moves it.
    """
    lines = sorted(
        f"{label}\t{criterion_hash(text)}"
        for label, texts in _variants_by_label(description).items()
        for text in texts
    )
    return hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()


def compare_criteria(
    pins: ModelCriteriaPins, ticket_id: str, description: str
) -> tuple[tuple[ModelCriterionDrift, ...], ModelCriteriaAmendment | None]:
    """Every drift between the pins and the live description, and the amendment for it."""
    resolved = criteria_by_label(description)
    variants = _variants_by_label(description)
    live_hash = {label: criterion_hash(text) for label, text in resolved.items()}
    by_label: dict[str, list[ModelCriterionPin]] = {}
    for pin in pins.bindings:
        by_label.setdefault(pin.label, []).append(pin)

    drift: list[ModelCriterionDrift] = []
    ordered = list(dict.fromkeys([*pins.known_labels, *by_label, *resolved, *variants]))
    for label in ordered:
        label_pins = by_label.get(label, [])
        if label not in resolved:
            if label in pins.known_labels or label_pins:
                drift.append(
                    ModelCriterionDrift(
                        kind=EnumCriteriaDriftKind.DELETED,
                        label=label,
                        item_ids=tuple(dict.fromkeys(p.item_id for p in label_pins)),
                        pinned_hash=label_pins[0].criterion_hash
                        if label_pins
                        else None,
                    )
                )
            continue
        current = live_hash[label]
        if len(variants.get(label, [])) > 1:
            drift.append(
                ModelCriterionDrift(
                    kind=EnumCriteriaDriftKind.DUPLICATE_LABEL,
                    label=label,
                    item_ids=tuple(dict.fromkeys(p.item_id for p in label_pins)),
                    pinned_hash=label_pins[0].criterion_hash if label_pins else None,
                    live_hash=current,
                )
            )
            continue
        if label not in pins.known_labels and not label_pins:
            drift.append(
                ModelCriterionDrift(
                    kind=EnumCriteriaDriftKind.ADDED, label=label, live_hash=current
                )
            )
            continue
        stale = [p for p in label_pins if p.criterion_hash != current]
        re_accepted = any(
            p.criterion_hash == current and p.accepted for p in label_pins
        )
        if stale and not re_accepted:
            drift.append(
                ModelCriterionDrift(
                    kind=EnumCriteriaDriftKind.EDITED,
                    label=label,
                    item_ids=tuple(dict.fromkeys(p.item_id for p in stale)),
                    pinned_hash=stale[0].criterion_hash,
                    live_hash=current,
                )
            )

    if not drift:
        return (), None
    revision = criteria_revision(description)

    def entries(kind: EnumCriteriaDriftKind) -> tuple[ModelCriteriaAmendmentEntry, ...]:
        return tuple(
            ModelCriteriaAmendmentEntry(
                label=d.label,
                item_ids=d.item_ids,
                from_hash=d.pinned_hash,
                to_hash=d.live_hash,
            )
            for d in drift
            if d.kind is kind
        )

    return tuple(drift), ModelCriteriaAmendment(
        ticket_id=ticket_id,
        criteria_revision=revision,
        rebind=entries(EnumCriteriaDriftKind.EDITED),
        retire=entries(EnumCriteriaDriftKind.DELETED),
        add=entries(EnumCriteriaDriftKind.ADDED),
        disambiguate=entries(EnumCriteriaDriftKind.DUPLICATE_LABEL),
    )


def evaluate_criteria(
    pins: ModelCriteriaPins,
    ticket_id: str,
    before: ModelTicketCriteriaRead,
    after: ModelTicketCriteriaRead,
) -> ModelCriteriaCheck:
    """Compare the pins with the ticket read before the checks ran and again after.

    Either read being unreadable refuses; the revision moving between them refuses,
    because the checks then ran against a criterion that was not the one the verdict
    would name. The comparison itself uses the read taken before the checks.
    """
    unavailable = before.unavailable_reason or after.unavailable_reason
    if before.description is None or after.description is None:
        return ModelCriteriaCheck(
            unavailable_reason=unavailable or "ticket unreadable",
            known_labels=pins.known_labels,
        )
    drift, amendment = compare_criteria(pins, ticket_id, before.description)
    revision = criteria_revision(before.description)
    revision_after = criteria_revision(after.description)
    return ModelCriteriaCheck(
        criteria_revision=revision,
        drift=drift,
        amendment=amendment,
        changed_during_verification=revision != revision_after,
        revision_after=revision_after,
        known_labels=pins.known_labels,
    )


def invalidate_drifted_results(
    results: Sequence[ModelEvidenceCheckResult], check: ModelCriteriaCheck | None
) -> list[ModelEvidenceCheckResult]:
    """Results whose PASS rests on a criterion that moved stop counting.

    A VERIFIED result that binds an invalidated label becomes SKIPPED and says why;
    nothing else changes, so a FAILED result still fails the verdict and an unbound
    result is untouched.
    """
    if check is None or not check.invalidated_labels:
        return list(results)
    invalidated = check.invalidated_labels
    out: list[ModelEvidenceCheckResult] = []
    for result in results:
        bound = {canonical_ac_label(label) for label in result.binds_ac}
        if result.status is EnumEvidenceCheckStatus.VERIFIED and bound & invalidated:
            moved = ", ".join(sorted(bound & invalidated))
            out.append(
                result.model_copy(
                    update={
                        "status": EnumEvidenceCheckStatus.SKIPPED,
                        "message": (
                            f"CRITERIA_DRIFT: this check passed, but it binds {moved}, "
                            "whose criterion on the ticket is no longer the one it was "
                            "accepted against; the pass does not count until a second "
                            f"lane re-accepts it. {result.message or ''}"
                        ).strip(),
                    }
                )
            )
        else:
            out.append(result)
    return out
