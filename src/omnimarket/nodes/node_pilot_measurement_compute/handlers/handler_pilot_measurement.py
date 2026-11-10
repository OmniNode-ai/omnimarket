# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerPilotMeasurement: piece 16's measurement of the pilots against their declared bars (OMN-19998, OMN-23611).

The judgement of one pilot is node_pilot_manifest_check_compute's (OMN-22979): a bar frozen before the start whose hash
matches the declared bar (refused otherwise), then the results held against the bar (accepted or rejected). This handler runs
that check per declared pilot and adds what the reading needs and the check does not hold: the result deadline and which bars
gate the reading. It reads no clock, file, network or environment; the request carries the time.

* unread facts: ``facts_unread_reason`` set reads UNKNOWN with one ``facts-unread:<token>`` reason; never an empty answer.
* no declarations: UNKNOWN with ``no-pilot-declared``.
* a declaration without a fact has not started: it counts in ``pilots_declared`` only and fails nothing. A fact whose
  pilot id matches no declaration is no evidence of any pilot and is ignored.
* a started pilot the check refuses (no bar hash, a hash that is not the declared bar's, a bar frozen after the start) reads
  ``pilot-refused:<id>:<findings joined by +>``.
* a started pilot with no result: past its declared deadline it reads ``pilot-no-result:<id>``; inside it the pilot waits in
  ``pending`` and fails nothing.
* a pilot the check rejects: each failed metric that gates the reading reads ``pilot-bar-missed:<id>:<metric>``; when no
  failed metric gates, the pilot is only ``rejected`` as a pilot (plan 7.2) and the reading is not at fault.

Any reason reads NOT_WORKING; else no pilot started reads UNKNOWN with ``no-pilot-started``; else WORKING.
"""

from __future__ import annotations

import re
import uuid

from omnimarket.models.model_pilot_measurement import (
    ModelPilotDeclaration,
    ModelPilotFact,
    ModelPilotMeasurementRequest,
    ModelPilotMeasurementResult,
)
from omnimarket.models.model_reading import EnumReadingVerdict
from omnimarket.nodes.node_pilot_manifest_check_compute.handlers.handler_pilot_manifest_check import (
    HandlerPilotManifestCheck,
)
from omnimarket.nodes.node_pilot_manifest_check_compute.models.enum_pilot_status import (
    EnumPilotStatus,
)
from omnimarket.nodes.node_pilot_manifest_check_compute.models.model_pilot_manifest import (
    ModelPilotManifest,
)
from omnimarket.nodes.node_pilot_manifest_check_compute.models.model_pilot_manifest_check_overlay import (
    ModelPilotManifestCheckOverlay,
)
from omnimarket.nodes.node_pilot_manifest_check_compute.models.model_pilot_manifest_check_request import (
    ModelPilotManifestCheckRequest,
)

_UNSAFE = re.compile(r"[^a-z0-9_.:-]+")


def token_safe(reason: str) -> str:
    """The reason as a token: lowercase, every run outside ``[a-z0-9_.:-]`` collapsed to ``-``, cut to 80."""
    return _UNSAFE.sub("-", reason.lower())[:80]


def newest_facts(request: ModelPilotMeasurementRequest) -> dict[str, ModelPilotFact]:
    """The fact of each pilot; of several for one pilot, the one that started last."""
    by_pilot: dict[str, ModelPilotFact] = {}
    for fact in request.facts:
        same = by_pilot.get(fact.pilot_id)
        if same is None or fact.started_at > same.started_at:
            by_pilot[fact.pilot_id] = fact
    return by_pilot


class _Tally:
    def __init__(self) -> None:
        self.reasons: list[str] = []
        self.pending: list[str] = []
        self.rejected: list[str] = []
        self.accepted: list[str] = []
        self.started = 0
        self.resulted = 0


def judge_pilot(
    checker: HandlerPilotManifestCheck,
    declaration: ModelPilotDeclaration,
    fact: ModelPilotFact,
    now_seconds_since_start: float,
    tally: _Tally,
) -> None:
    """Run the manifest check for one started pilot and fold its status into the tally."""
    pilot = declaration.pilot_id
    tally.started += 1
    checked = checker.handle(
        ModelPilotManifestCheckRequest(
            correlation_id=uuid.uuid5(
                uuid.NAMESPACE_URL, f"{pilot}|{fact.started_at.isoformat()}"
            ),
            manifest=ModelPilotManifest(
                pilot=pilot,
                bar=declaration.bar,
                bar_hash=fact.bar_hash,
                frozen_at=fact.frozen_at,
                started_at=fact.started_at,
            ),
            results=fact.results,
        )
    )
    if checked.status is EnumPilotStatus.REFUSED:
        findings = "+".join(finding.value for finding in checked.findings)
        tally.reasons.append(f"pilot-refused:{pilot}:{findings}")
    elif checked.status is EnumPilotStatus.READY:
        if now_seconds_since_start > declaration.result_deadline_seconds:
            tally.reasons.append(f"pilot-no-result:{pilot}")
        else:
            tally.pending.append(pilot)
    elif checked.status is EnumPilotStatus.ACCEPTED:
        tally.resulted += 1
        tally.accepted.append(pilot)
    else:
        tally.resulted += 1
        gating = [m for m in checked.failed_metrics if m in declaration.gating_metrics]
        tally.reasons.extend(f"pilot-bar-missed:{pilot}:{metric}" for metric in gating)
        if not gating:
            tally.rejected.append(pilot)


class HandlerPilotMeasurement:
    """COMPUTE handler: no clock read (the request carries the time), no state, no I/O."""

    def __init__(
        self, check_overlay: ModelPilotManifestCheckOverlay | None = None
    ) -> None:
        self._checker = HandlerPilotManifestCheck(check_overlay)

    def handle(
        self, request: ModelPilotMeasurementRequest
    ) -> ModelPilotMeasurementResult:
        declared = len(request.declarations)
        if request.facts_unread_reason is not None:
            return ModelPilotMeasurementResult(
                verdict=EnumReadingVerdict.UNKNOWN,
                reasons=(f"facts-unread:{token_safe(request.facts_unread_reason)}",),
                pilots_declared=declared,
                pilots_started=0,
                pilots_resulted=0,
            )
        if not request.declarations:
            return ModelPilotMeasurementResult(
                verdict=EnumReadingVerdict.UNKNOWN,
                reasons=("no-pilot-declared",),
                pilots_declared=0,
                pilots_started=0,
                pilots_resulted=0,
            )
        facts = newest_facts(request)
        tally = _Tally()
        for declaration in request.declarations:
            fact = facts.get(declaration.pilot_id)
            if fact is not None:
                judge_pilot(
                    self._checker,
                    declaration,
                    fact,
                    (request.now - fact.started_at).total_seconds(),
                    tally,
                )
        reasons = list(tally.reasons)
        verdict = EnumReadingVerdict.WORKING
        if reasons:
            verdict = EnumReadingVerdict.NOT_WORKING
        elif tally.started == 0:
            verdict = EnumReadingVerdict.UNKNOWN
            reasons.append("no-pilot-started")
        return ModelPilotMeasurementResult(
            verdict=verdict,
            reasons=tuple(reasons),
            pilots_declared=declared,
            pilots_started=tally.started,
            pilots_resulted=tally.resulted,
            pending=tuple(tally.pending),
            rejected=tuple(tally.rejected),
            accepted=tuple(tally.accepted),
        )


__all__: list[str] = [
    "HandlerPilotMeasurement",
    "judge_pilot",
    "newest_facts",
    "token_safe",
]
