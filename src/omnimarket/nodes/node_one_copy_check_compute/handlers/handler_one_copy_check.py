# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One behaviour, one live implementation; one resource, every writer under its lock (OMN-19998, ROOT 8 of 2026-11-15).

On 2026-11-11 the lab fill's waste fix (omnimarket 6126) landed in a copy no unit called while the copy the throughput tick
imported kept sending owned and blocked tickets for 22 hours. Nothing said which copy was live. This check reads every installed
unit, tick and writer source the overlay names and answers it: a behaviour with two implementations referenced (multiple_live), none
(none_live) or a referenced one that is not where the fix landed (fix_not_live); the copies nothing runs; and a resource writer that
takes none of the resource's lock markers (writer_without_lock). A violation is a result the caller reads and a ``one_copy_violation``
failure on the floor's emission chain; a read that failed or returned nothing is a typed failure, never "no violation".

``decide_one_copy`` is pure: no I/O, no clock, deterministic in any input order.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Literal

from omnimarket.handlers.emission_sink import (
    ChainEmitter,
    InMemoryEmissionSink,
    ProtocolEmissionSink,
)
from omnimarket.handlers.node_overlay_reader import load_structured_overlay
from omnimarket.nodes.node_one_copy_check_compute.models import (
    ModelBehaviour,
    ModelBehaviourReading,
    ModelLiveImplementation,
    ModelOneCopyCheckOverlay,
    ModelOneCopyCheckRequest,
    ModelOneCopyCheckResult,
    ModelOneCopyFinding,
    ModelReferrer,
    ModelResource,
    ModelResourceReading,
)
from omnimarket.nodes.node_one_copy_check_compute.protocols.protocol_referrer_reader import (
    InMemoryReferrerReader,
    ProtocolReferrerReader,
    ReferrerReadError,
)

NODE_NAME = "node_one_copy_check_compute"
FAILURE_VIOLATION = "one_copy_violation"
FAILURE_UNREADABLE = "referrers_unreadable"
FAILURE_SILENT = "referrers_silent"
FINDING_MULTIPLE_LIVE = "multiple_live"
FINDING_NONE_LIVE = "none_live"
FINDING_FIX_NOT_LIVE = "fix_not_live"
FINDING_WRITER_WITHOUT_LOCK = "writer_without_lock"
_RUNNING_KINDS = frozenset({"unit", "tick"})


class OneCopyCheckError(RuntimeError):
    """The check could not read its referrers; ``code`` is the typed failure code emitted on the chain."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code


def load_overlay(environ: dict[str, str] | None = None) -> ModelOneCopyCheckOverlay:
    """The node overlay; a missing or broken one refuses naming the setting (no packaged value)."""
    return load_structured_overlay(NODE_NAME, ModelOneCopyCheckOverlay, environ)


def _read_behaviour(
    behaviour: ModelBehaviour, running: list[ModelReferrer]
) -> tuple[ModelBehaviourReading, list[ModelOneCopyFinding]]:
    live: list[ModelLiveImplementation] = []
    unreferenced: list[str] = []
    for implementation in sorted(behaviour.implementations, key=lambda item: item.id):
        referrers = sorted(
            referrer.id
            for referrer in running
            if any(marker in referrer.text for marker in implementation.markers)
        )
        if referrers:
            live.append(
                ModelLiveImplementation(
                    id=implementation.id, referrers=tuple(referrers)
                )
            )
        else:
            unreferenced.append(implementation.id)
    live_ids = [item.id for item in live]
    findings: list[ModelOneCopyFinding] = []
    if len(live) > 1:
        detail = "; ".join(
            f"{item.id} run by {', '.join(item.referrers)}" for item in live
        )
        findings.append(
            ModelOneCopyFinding(
                code=FINDING_MULTIPLE_LIVE, subject=behaviour.name, detail=detail
            )
        )
    if not live:
        findings.append(
            ModelOneCopyFinding(
                code=FINDING_NONE_LIVE,
                subject=behaviour.name,
                detail="no installed unit or tick references any implementation",
            )
        )
    if behaviour.fix_landed_in is not None and behaviour.fix_landed_in not in live_ids:
        findings.append(
            ModelOneCopyFinding(
                code=FINDING_FIX_NOT_LIVE,
                subject=behaviour.name,
                detail=f"the fix landed in {behaviour.fix_landed_in}; live: {', '.join(live_ids) or 'none'}",
            )
        )
    reading = ModelBehaviourReading(
        name=behaviour.name,
        live=tuple(live),
        unreferenced=tuple(unreferenced),
        fix_landed_in=behaviour.fix_landed_in,
    )
    return reading, findings


def _writes(resource: ModelResource, text: str) -> bool:
    for line in text.splitlines():
        stripped = line.strip()
        if any(stripped.startswith(prefix) for prefix in resource.comment_prefixes):
            continue
        if any(marker in line for marker in resource.write_markers):
            return True
    return False


def _read_resource(
    resource: ModelResource, referrers: list[ModelReferrer]
) -> tuple[ModelResourceReading, list[ModelOneCopyFinding]]:
    writers = sorted(
        referrer.id for referrer in referrers if _writes(resource, referrer.text)
    )
    unlocked = sorted(
        referrer.id
        for referrer in referrers
        if _writes(resource, referrer.text)
        and not any(marker in referrer.text for marker in resource.lock_markers)
    )
    findings = [
        ModelOneCopyFinding(
            code=FINDING_WRITER_WITHOUT_LOCK,
            subject=resource.name,
            detail=f"{referrer_id} writes the resource and takes none of its lock markers",
        )
        for referrer_id in unlocked
    ]
    return ModelResourceReading(
        name=resource.name, writers=tuple(writers), writers_without_lock=tuple(unlocked)
    ), findings


def decide_one_copy(
    overlay: ModelOneCopyCheckOverlay,
    referrers: tuple[ModelReferrer, ...] | list[ModelReferrer],
    as_of: datetime,
) -> ModelOneCopyCheckResult:
    """Pure decision of the one-copy check over the referrers read."""
    running = [referrer for referrer in referrers if referrer.kind in _RUNNING_KINDS]
    every = list(referrers)
    behaviours: list[ModelBehaviourReading] = []
    resources: list[ModelResourceReading] = []
    findings: list[ModelOneCopyFinding] = []
    for behaviour in sorted(overlay.behaviours, key=lambda item: item.name):
        reading, found = _read_behaviour(behaviour, running)
        behaviours.append(reading)
        findings.extend(found)
    for resource in sorted(overlay.resources, key=lambda item: item.name):
        resource_reading, found = _read_resource(resource, every)
        resources.append(resource_reading)
        findings.extend(found)
    return ModelOneCopyCheckResult(
        as_of=as_of,
        ok=not findings,
        behaviours=tuple(behaviours),
        resources=tuple(resources),
        findings=tuple(findings),
    )


class HandlerOneCopyCheck:
    """Read the referrers, decide, and put the verdict on the floor's emission chain."""

    def __init__(
        self,
        overlay: ModelOneCopyCheckOverlay,
        reader: ProtocolReferrerReader | None = None,
        clock: Callable[[], datetime] | None = None,
        sink: ProtocolEmissionSink | None = None,
    ) -> None:
        self._overlay = overlay
        self._reader = reader if reader is not None else InMemoryReferrerReader()
        self._clock = clock or (lambda: datetime.now(UTC))
        self._sink = sink if sink is not None else InMemoryEmissionSink()
        self._backend: Literal["in_memory", "real"] = (
            "in_memory" if isinstance(self._reader, InMemoryReferrerReader) else "real"
        )

    def handle(self, request: ModelOneCopyCheckRequest) -> ModelOneCopyCheckResult:
        as_of = request.as_of if request.as_of is not None else self._clock()
        chain = ChainEmitter(
            self._sink,
            node=NODE_NAME,
            piece=self._overlay.emission_piece,
            clock=self._clock,
            backend=self._backend,
            origin="probe",
        )
        chain.start()
        try:
            referrers = self._reader.read()
        except ReferrerReadError as exc:
            chain.failure(FAILURE_UNREADABLE, str(exc))
            raise OneCopyCheckError(FAILURE_UNREADABLE, str(exc)) from None
        if not referrers:
            detail = "the reader returned no unit, tick or source"
            chain.failure(FAILURE_SILENT, detail)
            raise OneCopyCheckError(FAILURE_SILENT, detail)
        result = decide_one_copy(self._overlay, referrers, as_of)
        if result.ok:
            chain.success(
                {
                    "behaviours": len(result.behaviours),
                    "resources": len(result.resources),
                }
            )
        else:
            codes = ",".join(sorted({finding.code for finding in result.findings}))
            chain.failure(
                FAILURE_VIOLATION, f"{len(result.findings)} findings: {codes}"
            )
        return result
