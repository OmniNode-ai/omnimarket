# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerHostBringupOverlay: one host's overlay facts resolved into per-target check observations.

Pure and deterministic: no clock, no file, no network, no shell. The caller supplies
the check part of the declared target rows (read from the bring-up orchestrator
contract with ``check_rows_from_contract``) and the host overlay (validated with
``load_host_overlay``). The result carries one observation per declared target,
sorted by target name, ready for the host bring-up plan compute.

A target's state comes from the overlay facts for its check subject:

* no fact for the subject: ``unknown``;
* every fact for the subject agrees: that state;
* facts that disagree: ``unknown``, so a conflicting reading never reads as ok.

The overlay is refused, naming the offender, when a fact names a subject no
declared row checks (a mistyped subject would otherwise drop a recorded reading
silently) or a target row is declared twice.
"""

from __future__ import annotations

from collections.abc import Mapping

from omnimarket.models.host_bringup import EnumObservedState
from omnimarket.nodes.node_host_bringup_overlay_compute.models.model_host_bringup_overlay import (
    EnumOverlayRefusalKind,
    EnumOverlayStatus,
    ModelHostBringupOverlayRequest,
    ModelHostBringupOverlayResult,
    ModelHostOverlay,
    ModelOverlayCheckRow,
    ModelOverlayRefusal,
    ModelResolvedObservation,
)


def check_rows_from_contract(
    contract: Mapping[str, object],
) -> tuple[ModelOverlayCheckRow, ...]:
    """Read the name and check of every ``targets`` row of a bring-up graph contract.

    A row with no name or check, or a check kind outside the enum, raises
    ``pydantic.ValidationError`` (or ``ValueError`` when ``targets`` is not a list).
    """
    targets = contract.get("targets")
    if not isinstance(targets, list):
        raise ValueError("contract has no targets list")
    rows: list[ModelOverlayCheckRow] = []
    for row in targets:
        if not isinstance(row, Mapping):
            raise ValueError("a targets row is not a mapping")
        check = row.get("check")
        if not isinstance(check, Mapping):
            raise ValueError(f"target {row.get('name')!r} has no check mapping")
        rows.append(
            ModelOverlayCheckRow.model_validate(
                {
                    "target": row.get("name"),
                    "kind": check.get("kind"),
                    "subject": check.get("subject"),
                }
            )
        )
    return tuple(rows)


def load_host_overlay(data: Mapping[str, object]) -> ModelHostOverlay:
    """Validate one host's overlay data; a malformed fact raises ``pydantic.ValidationError``."""
    return ModelHostOverlay.model_validate(dict(data))


class HandlerHostBringupOverlay:
    """Resolve a host overlay against the declared check rows."""

    def handle(
        self, request: ModelHostBringupOverlayRequest
    ) -> ModelHostBringupOverlayResult:
        host = request.overlay.host
        refusals: list[ModelOverlayRefusal] = []

        by_target: dict[str, ModelOverlayCheckRow] = {}
        dupes: set[str] = set()
        for row in request.checks:
            if row.target in by_target:
                dupes.add(row.target)
            else:
                by_target[row.target] = row
        for name in sorted(dupes):
            refusals.append(
                ModelOverlayRefusal(
                    kind=EnumOverlayRefusalKind.DUPLICATE_TARGET,
                    names=(name,),
                    message=f"target {name} is declared more than once",
                )
            )

        subjects = {row.subject for row in by_target.values()}
        states: dict[str, set[EnumObservedState]] = {}
        evidence: dict[str, set[str]] = {}
        for fact in request.overlay.facts:
            states.setdefault(fact.subject, set()).add(fact.state)
            evidence.setdefault(fact.subject, set()).add(fact.evidence)
        for subject in sorted(set(states) - subjects):
            refusals.append(
                ModelOverlayRefusal(
                    kind=EnumOverlayRefusalKind.UNKNOWN_SUBJECT,
                    names=(subject,),
                    message=(
                        f"host {host} records a fact for {subject}, "
                        "which no declared target checks"
                    ),
                )
            )

        if refusals:
            return ModelHostBringupOverlayResult(
                status=EnumOverlayStatus.REFUSED, host=host, refusals=tuple(refusals)
            )

        observations: list[ModelResolvedObservation] = []
        for name in sorted(by_target):
            subject = by_target[name].subject
            seen = states.get(subject, set())
            state = next(iter(seen)) if len(seen) == 1 else EnumObservedState.UNKNOWN
            observations.append(
                ModelResolvedObservation(
                    target=name,
                    subject=subject,
                    state=state,
                    evidence=tuple(sorted(evidence.get(subject, set()))),
                )
            )
        return ModelHostBringupOverlayResult(
            status=EnumOverlayStatus.RESOLVED,
            host=host,
            observations=tuple(observations),
        )


__all__ = [
    "HandlerHostBringupOverlay",
    "check_rows_from_contract",
    "load_host_overlay",
]
