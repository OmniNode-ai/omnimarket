# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Resolve audited AC proposal retirements against the original contract."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from pydantic import ValidationError

from omnimarket.nodes.node_dod_verify.models.model_ac_binding_retirement import (
    ModelAcBindingRetirement,
    ModelAcBindingRetirementResolution,
)
from omnimarket.nodes.node_dod_verify.services.ac_falsifier_checks import (
    _canonical_label,
    _self_accepted_binding_records,
)


def _record_labels(item: Mapping[str, Any]) -> frozenset[str]:
    records = item.get("ac_bindings")
    if not isinstance(records, list):
        return frozenset()
    return frozenset(
        _canonical_label(label)
        for record in records
        if isinstance(record, Mapping) and isinstance(label := record.get("label"), str)
    )


def _claimed_labels(item: Mapping[str, Any]) -> frozenset[str]:
    binds = item.get("binds_ac")
    return _record_labels(item) | frozenset(
        _canonical_label(label)
        for label in (binds if isinstance(binds, (list, tuple)) else ())
        if isinstance(label, str)
    )


def resolve_retirements(dod_items: Sequence[Any]) -> ModelAcBindingRetirementResolution:
    """Validate all entries before withdrawing any claims; never alter the items.

    The first occurrence of a target/label owns that pair. Later duplicate
    entries are ignored, including when the first occurrence is refused.
    Conflicts are tested against the entire candidate set, so withdrawing a
    superseder refuses both entries, including mutual cycles and chains.
    """
    by_id = {
        item["id"]: item
        for item in dod_items
        if isinstance(item, Mapping) and isinstance(item.get("id"), str)
    }
    unaccepted = {
        (item_id, _canonical_label(label))
        for item_id, label, _ in _self_accepted_binding_records(dod_items)
    }
    candidates: dict[tuple[str, str], tuple[int, ModelAcBindingRetirement]] = {}
    refusals: dict[int, str] = {}
    seen: set[tuple[str, str]] = set()
    order = 0

    def refuse(
        index: int, carrier: str, item: str, label: str, code: str, why: str
    ) -> None:
        refusals[index] = f"{carrier}:{item}:{label} {code}: {why}"

    for carrier_index, carrier in enumerate(dod_items):
        if not isinstance(carrier, Mapping):
            continue
        entries = carrier.get("supersedes_ac_binding")
        if not isinstance(entries, list):
            continue
        carrier_id = str(carrier.get("id") or f"dod_evidence[{carrier_index}]")
        for raw in entries:
            index = order
            order += 1
            target_id = (
                str(raw.get("item", "<unknown>"))
                if isinstance(raw, Mapping)
                else "<unknown>"
            )
            label = (
                str(raw.get("label", "<unknown>"))
                if isinstance(raw, Mapping)
                else "<unknown>"
            )
            pair = (target_id, _canonical_label(label))
            if (
                isinstance(raw, Mapping)
                and isinstance(raw.get("item"), str)
                and isinstance(raw.get("label"), str)
            ):
                if pair in seen:
                    continue
                seen.add(pair)
            if isinstance(raw, Mapping) and "reason_kind" not in raw:
                refuse(
                    index,
                    carrier_id,
                    target_id,
                    label,
                    "RETIREMENT_UNTYPED",
                    "missing reason_kind and typed audit fields",
                )
                continue
            try:
                # carried_by is derived from the carrier, never trusted from an entry.
                if not isinstance(raw, Mapping) or "carried_by" in raw:
                    raise ValueError("entry must be an object without carried_by")
                entry = ModelAcBindingRetirement.model_validate(
                    {**raw, "carried_by": carrier_id}
                )
            except (ValidationError, ValueError) as exc:
                why = str(exc)
                if isinstance(exc, ValidationError):
                    error = exc.errors()[0]
                    why = f"{'.'.join(map(str, error['loc']))}: {error['msg']}"
                refuse(index, carrier_id, target_id, label, "RETIREMENT_INVALID", why)
                continue
            target = by_id.get(entry.item)
            code: str | None = None
            why = ""
            if target is None:
                code, why = "RETIREMENT_TARGET_UNKNOWN", "target item does not exist"
            elif pair[1] not in _claimed_labels(target):
                code, why = (
                    "RETIREMENT_LABEL_NOT_BOUND",
                    "target does not claim this label",
                )
            elif pair not in unaccepted and pair[1] in _record_labels(target):
                code, why = (
                    "RETIREMENT_BINDING_ACCEPTED",
                    "binding is independently accepted",
                )
            elif carrier_id == entry.item:
                code, why = (
                    "RETIREMENT_SELF_CARRIED",
                    "target cannot carry its own retirement",
                )
            elif entry.superseded_by is not None:
                superseder = by_id.get(entry.superseded_by)
                if (
                    superseder is None
                    or entry.superseded_by == entry.item
                    or pair[1] not in _claimed_labels(superseder)
                ):
                    code, why = (
                        "RETIREMENT_SUPERSEDER_INVALID",
                        "superseder must be another existing item claiming this label",
                    )
            if code is not None:
                refuse(index, carrier_id, target_id, label, code, why)
            else:
                candidates[pair] = (index, entry)

    for (_, label), (index, entry) in candidates.items():
        if entry.superseded_by is None:
            continue
        other = candidates.get((entry.superseded_by, label))
        if other is None:
            continue
        for conflict_index, conflict in ((index, entry), other):
            refuse(
                conflict_index,
                conflict.carried_by,
                conflict.item,
                conflict.label,
                "RETIREMENT_SUPERSEDER_INVALID",
                "superseder's binding is also a retirement candidate",
            )

    return ModelAcBindingRetirementResolution(
        applied=tuple(
            entry for index, entry in candidates.values() if index not in refusals
        ),
        refused=tuple(refusals[index] for index in sorted(refusals)),
    )
