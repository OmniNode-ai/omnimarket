# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerContextInstallConfirm: the plan section 6 boundary algorithm (OMN-21115, TA.13).

Pure: ``handle(ModelInstallConfirmRequest) -> ModelInstallConfirmResult``, no I/O.

1. The active chain is the walk from the newest captured record back through
   ``parentUuid``; a boundary has ``parentUuid: null`` and reaches the chain
   before it through ``logicalParentUuid``. The current boundary is the first
   ``compact_boundary`` met on the walk.
2. A boundary's replacement run is the records descending from it through
   ``parentUuid`` (preferring the child on the active chain), ending before a
   record listed in ``compactMetadata.preservedMessages.uuids``, a meta record
   (the ``<local-command-caveat>`` that ``/compact`` appends), an operator
   prompt (``promptSource`` set) or another boundary.
3. ``observed_hash`` is sha256 over the canonical JSON of the run's
   ``{role, text}`` list, hashed as the compaction orchestrator hashes
   ``installed_messages_hash``.
4. Boundaries are judged in capture order, each against the activations
   published since the previous boundary. The activation line on the last line
   of the run's last message names the activation. Installation facts are first
   derived and never revised by a later boundary.
5. The current context is the newest boundary on the active chain and what it
   installed; an activation installed at an earlier boundary on that chain is
   retired at the next one.

Records and activations are sorted and de-duplicated on entry, so the result is
a function of the sets received, not of their arrival order.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable
from itertools import pairwise
from uuid import UUID

from omnimarket.models.context_install_confirm import (
    EnumBoundaryKind,
    EnumInstallFact,
    EnumInstallFactReason,
    ModelActivationRetirement,
    ModelBoundaryReading,
    ModelCapturedTranscriptRecord,
    ModelCurrentContext,
    ModelInstallationFact,
    ModelInstallConfirmRequest,
    ModelInstallConfirmResult,
    ModelPublishedActivation,
)
from omnimarket.models.context_pack_render import ModelContextPackRenderMessage

_ACTIVATION_LINE = re.compile(
    r"^activation_id: ([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})$"
)
_MESSAGE_TYPES = frozenset({"user", "assistant"})


def observed_messages_hash(messages: Iterable[ModelContextPackRenderMessage]) -> str:
    """sha256 over the canonical JSON of the role/text list (the orchestrator's form)."""
    raw = json.dumps(
        [m.model_dump() for m in messages],
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _activation_line(text: str) -> UUID | None:
    last_line = text.rstrip("\n").rsplit("\n", 1)[-1]
    match = _ACTIVATION_LINE.match(last_line)
    return UUID(match.group(1)) if match else None


class _Session:
    """The de-duplicated, time-ordered records of one session and their links."""

    def __init__(self, records: Iterable[ModelCapturedTranscriptRecord]) -> None:
        by_uuid: dict[str, ModelCapturedTranscriptRecord] = {}
        for record in sorted(records, key=lambda r: (r.timestamp, r.uuid)):
            by_uuid.setdefault(record.uuid, record)
        self.by_uuid = by_uuid
        self.ordered = list(by_uuid.values())
        self.children: dict[str, list[ModelCapturedTranscriptRecord]] = {}
        for record in self.ordered:
            if record.parent_uuid is not None:
                self.children.setdefault(record.parent_uuid, []).append(record)
        self.chain_boundaries, self.active = self._active_chain()

    def _walk_to_root(
        self, start: ModelCapturedTranscriptRecord, seen: set[str]
    ) -> ModelCapturedTranscriptRecord | None:
        """Walk ``parentUuid`` from ``start``; return the boundary that ends the walk, if any."""
        current: ModelCapturedTranscriptRecord | None = start
        while current is not None and current.uuid not in seen:
            seen.add(current.uuid)
            if current.is_boundary:
                return current
            parent = current.parent_uuid
            current = self.by_uuid.get(parent) if parent is not None else None
        return None

    def _active_chain(
        self,
    ) -> tuple[list[ModelCapturedTranscriptRecord], frozenset[str]]:
        if not self.ordered:
            return [], frozenset()
        seen: set[str] = set()
        boundaries: list[ModelCapturedTranscriptRecord] = []
        boundary = self._walk_to_root(self.ordered[-1], seen)
        while boundary is not None:
            boundaries.append(boundary)
            logical = boundary.logical_parent_uuid
            before = self.by_uuid.get(logical) if logical is not None else None
            boundary = self._walk_to_root(before, seen) if before is not None else None
        boundaries.reverse()
        return boundaries, frozenset(seen)

    def replacement_run(
        self, boundary: ModelCapturedTranscriptRecord
    ) -> list[ModelCapturedTranscriptRecord]:
        preserved = set(boundary.preserved_uuids)
        run: list[ModelCapturedTranscriptRecord] = []
        current = boundary
        visited = {boundary.uuid}
        while True:
            kids = self.children.get(current.uuid, [])
            if not kids:
                return run
            nxt = next((k for k in kids if k.uuid in self.active), kids[0])
            if (
                nxt.uuid in visited
                or nxt.uuid in preserved
                or nxt.is_meta
                or nxt.is_boundary
                or (nxt.record_type == "user" and nxt.prompt_source is not None)
            ):
                return run
            visited.add(nxt.uuid)
            run.append(nxt)
            current = nxt


def _read_boundary(
    session: _Session, boundary: ModelCapturedTranscriptRecord
) -> ModelBoundaryReading:
    run = session.replacement_run(boundary)
    messages = [r for r in run if r.record_type in _MESSAGE_TYPES]
    observed: str | None = None
    if messages and all(r.text is not None for r in messages):
        observed = observed_messages_hash(
            ModelContextPackRenderMessage(
                role=r.role or r.record_type, text=r.text or ""
            )
            for r in messages
        )
    uuids = tuple(r.uuid for r in run)
    if not messages:
        return ModelBoundaryReading(
            boundary_uuid=boundary.uuid,
            kind=EnumBoundaryKind.NATIVE,
            replacement_uuids=uuids,
        )
    trailer = messages[-1].text
    if trailer is None:
        return ModelBoundaryReading(
            boundary_uuid=boundary.uuid,
            kind=EnumBoundaryKind.UNREADABLE,
            replacement_uuids=uuids,
        )
    activation_id = _activation_line(trailer)
    return ModelBoundaryReading(
        boundary_uuid=boundary.uuid,
        kind=EnumBoundaryKind.CUSTOM if activation_id else EnumBoundaryKind.NATIVE,
        activation_id=activation_id,
        observed_hash=observed,
        replacement_uuids=uuids,
    )


def _fact(
    activation: ModelPublishedActivation,
    fact: EnumInstallFact,
    *,
    reason: EnumInstallFactReason | None = None,
    boundary_uuid: str | None = None,
    observed_hash: str | None = None,
) -> ModelInstallationFact:
    return ModelInstallationFact(
        activation_id=activation.activation_id,
        fact=fact,
        reason=reason,
        boundary_uuid=boundary_uuid,
        installed_messages_hash=activation.installed_messages_hash,
        observed_hash=observed_hash,
    )


class HandlerContextInstallConfirm:
    """handle(ModelInstallConfirmRequest) -> ModelInstallConfirmResult."""

    def handle(self, request: ModelInstallConfirmRequest) -> ModelInstallConfirmResult:
        session = _Session(request.records)
        activations: dict[UUID, ModelPublishedActivation] = {}
        for activation in sorted(
            request.activations, key=lambda a: (a.published_at, str(a.activation_id))
        ):
            activations.setdefault(activation.activation_id, activation)

        boundaries = [r for r in session.ordered if r.is_boundary]
        readings = {b.uuid: _read_boundary(session, b) for b in boundaries}
        facts: dict[UUID, ModelInstallationFact] = {}
        previous_at = None
        for boundary in boundaries:
            reading = readings[boundary.uuid]
            window = [
                a
                for a in activations.values()
                if a.activation_id not in facts
                and (previous_at is None or a.published_at > previous_at)
                and a.published_at <= boundary.timestamp
            ]
            named = reading.activation_id
            if reading.kind is EnumBoundaryKind.CUSTOM and named is not None:
                target = activations.get(named)
                if target is not None and named not in facts:
                    if reading.observed_hash is None:
                        facts[named] = _fact(
                            target,
                            EnumInstallFact.UNCERTAIN,
                            reason=EnumInstallFactReason.UNPARSEABLE_RECORD,
                            boundary_uuid=boundary.uuid,
                        )
                    elif reading.observed_hash == target.installed_messages_hash:
                        facts[named] = _fact(
                            target,
                            EnumInstallFact.INSTALLED,
                            boundary_uuid=boundary.uuid,
                            observed_hash=reading.observed_hash,
                        )
                    else:
                        facts[named] = _fact(
                            target,
                            EnumInstallFact.INSTALL_MISMATCH,
                            boundary_uuid=boundary.uuid,
                            observed_hash=reading.observed_hash,
                        )
                undecided_fact = EnumInstallFact.NOT_INSTALLED
                undecided_reason = EnumInstallFactReason.NOT_SELECTED
            elif reading.kind is EnumBoundaryKind.NATIVE:
                undecided_fact = EnumInstallFact.NOT_INSTALLED
                undecided_reason = EnumInstallFactReason.NATIVE_COMPACTION
            else:
                undecided_fact = EnumInstallFact.UNCERTAIN
                undecided_reason = EnumInstallFactReason.UNPARSEABLE_RECORD
            for activation in window:
                if activation.activation_id not in facts:
                    facts[activation.activation_id] = _fact(
                        activation,
                        undecided_fact,
                        reason=undecided_reason,
                        boundary_uuid=boundary.uuid,
                    )
            previous_at = boundary.timestamp

        pending: list[UUID] = []
        for activation in activations.values():
            if activation.activation_id in facts:
                continue
            newest = request.newest_captured_at
            if newest is not None and newest > activation.published_at:
                facts[activation.activation_id] = _fact(
                    activation,
                    EnumInstallFact.UNCERTAIN,
                    reason=EnumInstallFactReason.CAPTURE_GAP,
                )
            else:
                pending.append(activation.activation_id)

        return ModelInstallConfirmResult(
            session_id=request.session_id,
            boundaries=tuple(readings[b.uuid] for b in boundaries),
            facts=tuple(sorted(facts.values(), key=lambda f: str(f.activation_id))),
            pending_activation_ids=tuple(sorted(pending, key=str)),
            current=_current_context(session, readings, facts),
        )


def _current_context(
    session: _Session,
    readings: dict[str, ModelBoundaryReading],
    facts: dict[UUID, ModelInstallationFact],
) -> ModelCurrentContext:
    chain = session.chain_boundaries
    if not chain:
        return ModelCurrentContext()

    def installed_at(boundary_uuid: str) -> UUID | None:
        named = readings[boundary_uuid].activation_id
        fact = facts.get(named) if named is not None else None
        if (
            fact is not None
            and fact.fact is EnumInstallFact.INSTALLED
            and fact.boundary_uuid == boundary_uuid
        ):
            return fact.activation_id
        return None

    retirements: list[ModelActivationRetirement] = []
    for older, newer in pairwise(chain):
        retired = installed_at(older.uuid)
        if retired is not None:
            retirements.append(
                ModelActivationRetirement(
                    activation_id=retired,
                    installed_at_boundary_uuid=older.uuid,
                    retired_at_boundary_uuid=newer.uuid,
                )
            )
    current = chain[-1]
    return ModelCurrentContext(
        boundary_uuid=current.uuid,
        kind=readings[current.uuid].kind,
        activation_id=installed_at(current.uuid),
        chain_boundary_uuids=tuple(b.uuid for b in chain),
        retirements=tuple(retirements),
    )


__all__ = ["HandlerContextInstallConfirm", "observed_messages_hash"]
