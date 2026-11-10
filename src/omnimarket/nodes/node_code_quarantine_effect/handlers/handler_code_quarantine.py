# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Quarantine an unused entity with its tests, read the build and the lab start without it, then remove it.

Quarantine takes the entity and its tests out of the build and starts a clock; nothing is deleted. Before the
clock starts the build and the lab runtime start are read once without the entity, and a red one puts it back.
A check reads the build, the lab start and the entity's reading again: any red, or a missing reading, restores
the entity and resets the clock. A removal is accepted only for entities whose clock has run its days, and only
with a git tag that exists and a catalog row (entity, tag, sha, reason) for every entity in the batch. Every end
is a typed outcome and is reported once, as a typed event under the run id.
"""

from __future__ import annotations

from datetime import timedelta

from omnimarket.nodes.node_code_quarantine_effect.models import (
    EnumEntityState,
    EnumFailedGate,
    EnumQuarantineMode,
    EnumQuarantineOutcome,
    ModelCatalogRow,
    ModelPolicy,
    ModelQuarantineEvent,
    ModelQuarantineRecord,
    ModelQuarantineRequest,
    ModelQuarantineResult,
)
from omnimarket.nodes.node_code_quarantine_effect.protocols import (
    ProtocolClock,
    ProtocolGates,
    ProtocolQuarantineReporter,
    ProtocolQuarantineStore,
    ProtocolRepository,
    QuarantineStepError,
)


class HandlerCodeQuarantine:
    def __init__(
        self,
        *,
        policy: ModelPolicy,
        store: ProtocolQuarantineStore,
        repository: ProtocolRepository,
        gates: ProtocolGates,
        clock: ProtocolClock,
        reporter: ProtocolQuarantineReporter,
    ) -> None:
        self._policy = policy
        self._store = store
        self._repo = repository
        self._gates = gates
        self._clock = clock
        self._reporter = reporter

    def handle(self, request: ModelQuarantineRequest) -> ModelQuarantineResult:
        result = self._run(request)
        self._reporter.report(
            ModelQuarantineEvent(
                correlation_id=request.run_id,
                kind=f"code-quarantine.{request.mode.value}.{result.outcome.value}",
                repository=request.repository,
                entity=result.entity,
                outcome=result.outcome,
                detail=result.detail,
                reading=result.reading,
            )
        )
        return result

    def _result(
        self,
        request: ModelQuarantineRequest,
        outcome: EnumQuarantineOutcome,
        detail: str,
        *,
        entity: str | None = None,
        failed_gate: EnumFailedGate | None = None,
        removed: tuple[str, ...] = (),
        record: ModelQuarantineRecord | None = None,
        changed: bool = False,
    ) -> ModelQuarantineResult:
        try:
            excluded = self._store.excluded_paths(request.repository)
        except QuarantineStepError:
            excluded = ()
        return ModelQuarantineResult(
            run_id=request.run_id,
            mode=request.mode,
            repository=request.repository,
            entity=request.entity if entity is None else entity,
            outcome=outcome,
            detail=detail,
            failed_gate=failed_gate,
            excluded_paths=excluded,
            removed_entities=removed,
            clock_started_at=record.clock_started_at if record else None,
            changed=changed,
        )

    def _run(self, request: ModelQuarantineRequest) -> ModelQuarantineResult:
        try:
            if request.mode is EnumQuarantineMode.QUARANTINE:
                return self._quarantine(request)
            if request.mode is EnumQuarantineMode.CHECK:
                return self._check(request)
            return self._remove(request)
        except QuarantineStepError as exc:
            return self._result(
                request,
                EnumQuarantineOutcome.BACKEND_FAILED,
                f"{exc.step} step failed: {exc}",
            )

    def _failed_gate(
        self, repository: str, entity: str, excluded: tuple[str, ...]
    ) -> EnumFailedGate | None:
        if not self._gates.clean_build(repository, excluded):
            return EnumFailedGate.CLEAN_BUILD
        if not self._gates.lab_runtime_start(repository, excluded):
            return EnumFailedGate.LAB_RUNTIME_START
        if not self._gates.reading_present(repository, entity):
            return EnumFailedGate.MISSING_READING
        return None

    def _restore(
        self,
        request: ModelQuarantineRequest,
        record: ModelQuarantineRecord,
        gate: EnumFailedGate,
    ) -> ModelQuarantineResult:
        restored = record.model_copy(
            update={
                "state": EnumEntityState.RESTORED,
                "clock_started_at": None,
                "restored_reason": gate.value,
                "restarts": record.restarts + 1,
            }
        )
        self._store.put(restored)
        return self._result(
            request,
            EnumQuarantineOutcome.RESTORED,
            f"{gate.value} was red without {record.entity}; it is back in the build and its clock is reset",
            entity=record.entity,
            failed_gate=gate,
            record=restored,
            changed=True,
        )

    def _quarantine(self, request: ModelQuarantineRequest) -> ModelQuarantineResult:
        evidence = request.evidence
        assert evidence is not None
        if (
            not evidence.evidence_ref.strip()
            or evidence.observation_days < self._policy.min_observation_days
        ):
            return self._result(
                request,
                EnumQuarantineOutcome.REFUSED_NO_EVIDENCE,
                f"the observation reads {evidence.observation_days} days "
                f"(needs {self._policy.min_observation_days}) with evidence "
                f"{evidence.evidence_ref!r}; the entity stays with its tests",
            )
        if evidence.public_consumers:
            return self._result(
                request,
                EnumQuarantineOutcome.REFUSED_PUBLIC_CONSUMER,
                f"{evidence.public_consumers} public consumer(s) found; the entity stays",
            )
        current = self._store.get(request.repository, request.entity)
        if current is not None and current.state is EnumEntityState.QUARANTINED:
            return self._result(
                request,
                EnumQuarantineOutcome.REFUSED_ALREADY_QUARANTINED,
                f"{request.entity} is already quarantined; its clock was not touched",
                record=current,
            )
        paths = (*request.entity_paths, *request.test_paths)
        missing = [
            p for p in paths if not self._repo.path_exists(request.repository, p)
        ]
        if missing:
            return self._result(
                request,
                EnumQuarantineOutcome.REFUSED_UNKNOWN_PATH,
                f"not in the repository: {', '.join(missing)}",
            )
        started = ModelQuarantineRecord(
            repository=request.repository,
            entity=request.entity,
            state=EnumEntityState.QUARANTINED,
            entity_paths=request.entity_paths,
            test_paths=request.test_paths,
            clock_started_at=self._clock.now(),
            restarts=current.restarts if current else 0,
        )
        self._store.put(started)
        gate = self._failed_gate(
            request.repository,
            request.entity,
            self._store.excluded_paths(request.repository),
        )
        if gate is not None:
            return self._restore(request, started, gate)
        return self._result(
            request,
            EnumQuarantineOutcome.QUARANTINED,
            f"{request.entity} and {len(request.test_paths)} test path(s) are out of the build; "
            f"the files stay; the {self._policy.quarantine_days}-day clock started",
            record=started,
            changed=True,
        )

    def _check(self, request: ModelQuarantineRequest) -> ModelQuarantineResult:
        record = self._store.get(request.repository, request.entity)
        if record is None or record.state is not EnumEntityState.QUARANTINED:
            return self._result(
                request,
                EnumQuarantineOutcome.REFUSED_NOT_QUARANTINED,
                f"{request.entity} is not quarantined in {request.repository}",
                record=record,
            )
        gate = self._failed_gate(
            request.repository,
            request.entity,
            self._store.excluded_paths(request.repository),
        )
        if gate is not None:
            return self._restore(request, record, gate)
        assert record.clock_started_at is not None
        age = self._clock.now() - record.clock_started_at
        if age >= timedelta(days=self._policy.quarantine_days):
            return self._result(
                request,
                EnumQuarantineOutcome.DUE_FOR_REMOVAL,
                f"build, lab start and reading are green without {request.entity} after "
                f"{age.days} day(s); it may be removed with a tag and a catalog row",
                record=record,
            )
        return self._result(
            request,
            EnumQuarantineOutcome.STILL_QUARANTINED,
            f"build, lab start and reading are green without {request.entity}; "
            f"{age.days} of {self._policy.quarantine_days} days",
            record=record,
        )

    def _remove(self, request: ModelQuarantineRequest) -> ModelQuarantineResult:
        batch = request.batch
        assert batch is not None
        records: list[ModelQuarantineRecord] = []
        for entity in batch.entities:
            record = self._store.get(request.repository, entity)
            if record is None or record.state is not EnumEntityState.QUARANTINED:
                return self._result(
                    request,
                    EnumQuarantineOutcome.REFUSED_NOT_QUARANTINED,
                    f"{entity} is not quarantined in {request.repository}; nothing was removed",
                    entity=entity,
                )
            assert record.clock_started_at is not None
            age = self._clock.now() - record.clock_started_at
            if age < timedelta(days=self._policy.quarantine_days):
                return self._result(
                    request,
                    EnumQuarantineOutcome.REFUSED_CLOCK_RUNNING,
                    f"{entity} has been out {age.days} of {self._policy.quarantine_days} days; nothing was removed",
                    entity=entity,
                    record=record,
                )
            records.append(record)
        tagged = self._repo.tag_sha(request.repository, batch.tag) if batch.tag else ""
        if not tagged:
            return self._result(
                request,
                EnumQuarantineOutcome.REFUSED_NO_TAG,
                f"{request.repository} has no tag {batch.tag!r}; nothing was removed",
            )
        rows: dict[str, ModelCatalogRow] = {r.entity: r for r in batch.catalog_rows}
        for entity in batch.entities:
            row = rows.get(entity)
            if (
                row is None
                or row.tag != batch.tag
                or row.sha != tagged
                or not row.reason.strip()
            ):
                return self._result(
                    request,
                    EnumQuarantineOutcome.REFUSED_NO_CATALOG_ROW,
                    f"no catalog row (entity, tag {batch.tag}, sha {tagged}, reason) for {entity}; "
                    "nothing was removed",
                    entity=entity,
                )
        self._store.append_catalog(
            request.repository, tuple(rows[e] for e in batch.entities)
        )
        for record in records:
            self._repo.remove_paths(
                request.repository, (*record.entity_paths, *record.test_paths)
            )
            self._store.put(
                record.model_copy(
                    update={"state": EnumEntityState.REMOVED, "clock_started_at": None}
                )
            )
        return self._result(
            request,
            EnumQuarantineOutcome.REMOVED,
            f"{len(records)} entity(ies) removed with their tests under tag {batch.tag} at {tagged}",
            entity=",".join(batch.entities),
            removed=batch.entities,
            changed=True,
        )
