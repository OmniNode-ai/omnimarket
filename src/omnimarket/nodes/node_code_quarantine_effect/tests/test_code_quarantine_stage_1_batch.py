# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Stage 1 of the unused-code removal: one archived repository's leftovers go as one batch (OMN-23146 AC1).

Each entity of the batch is quarantined for 7 days with its tests, the build and the lab runtime start are read
without it, and only then is the batch removed with its tests under one tag and one catalog row per entity.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

import pytest

from omnimarket.nodes.node_code_quarantine_effect.adapters.adapters_memory import (
    InMemoryGates,
    InMemoryReporter,
    InMemoryRepository,
    InMemoryStore,
    SteppedClock,
)
from omnimarket.nodes.node_code_quarantine_effect.handlers.handler_code_quarantine import (
    HandlerCodeQuarantine,
)
from omnimarket.nodes.node_code_quarantine_effect.models import (
    EnumFailedGate,
    EnumQuarantineMode,
    EnumQuarantineOutcome,
    ModelCatalogRow,
    ModelPolicy,
    ModelQuarantineRequest,
    ModelRemovalBatch,
    ModelRemovalEvidence,
)

pytestmark = pytest.mark.unit

ARCHIVED = "archived_repo"
TAG = "stage-1-archived-repo"
SHA = "0123456789abcdef0123456789abcdef01234567"
START = datetime(2026, 11, 14, 12, 0, tzinfo=UTC)
LIVE = "src/live/app.py"
#: Entity -> (entity paths, test paths): a module, a package and a script, each with its tests.
LEFTOVERS: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    "old_module": (("src/old_module.py",), ("tests/test_old_module.py",)),
    "old_package": (
        ("src/old_pkg/__init__.py", "src/old_pkg/core.py"),
        ("tests/old_pkg/test_core.py",),
    ),
    "old_script": (("scripts/old_script.py",), ("tests/scripts/test_old_script.py",)),
}
ENTITIES = tuple(LEFTOVERS)
ALL_PATHS = {p for ents, tests in LEFTOVERS.values() for p in (*ents, *tests)}


@dataclass
class Batch:
    handler: HandlerCodeQuarantine
    clock: SteppedClock
    gates: InMemoryGates
    repository: InMemoryRepository
    store: InMemoryStore


def _batch() -> Batch:
    store = InMemoryStore()
    repository = InMemoryRepository(
        {ARCHIVED: {*ALL_PATHS, LIVE}}, {ARCHIVED: {TAG: SHA}}
    )
    gates = InMemoryGates(build=True, start=True, reading=True)
    clock = SteppedClock(START)
    return Batch(
        HandlerCodeQuarantine(
            policy=ModelPolicy(quarantine_days=7, min_observation_days=7),
            store=store,
            repository=repository,
            gates=gates,
            clock=clock,
            reporter=InMemoryReporter(),
        ),
        clock,
        gates,
        repository,
        store,
    )


def _quarantine(entity: str, consumers: int = 0) -> ModelQuarantineRequest:
    entity_paths, test_paths = LEFTOVERS[entity]
    return ModelQuarantineRequest(
        run_id=f"stage1-{entity}",
        mode=EnumQuarantineMode.QUARANTINE,
        repository=ARCHIVED,
        entity=entity,
        entity_paths=entity_paths,
        test_paths=test_paths,
        evidence=ModelRemovalEvidence(
            observation_days=7,
            evidence_ref="code-graph standing run, 7-day window",
            public_consumers=consumers,
        ),
    )


def _check(entity: str) -> ModelQuarantineRequest:
    return ModelQuarantineRequest(
        run_id=f"stage1-check-{entity}",
        mode=EnumQuarantineMode.CHECK,
        repository=ARCHIVED,
        entity=entity,
    )


def _remove(
    entities: tuple[str, ...] = ENTITIES, skip_row_for: str = ""
) -> ModelQuarantineRequest:
    rows = tuple(
        ModelCatalogRow(
            entity=e, tag=TAG, sha=SHA, reason="leftover of an archived repository"
        )
        for e in entities
        if e != skip_row_for
    )
    return ModelQuarantineRequest(
        run_id="stage1-remove",
        mode=EnumQuarantineMode.REMOVE,
        repository=ARCHIVED,
        batch=ModelRemovalBatch(tag=TAG, entities=entities, catalog_rows=rows),
    )


def _quarantine_all(batch: Batch) -> None:
    for entity in ENTITIES:
        result = batch.handler.handle(_quarantine(entity))
        assert result.outcome is EnumQuarantineOutcome.QUARANTINED


def test_stage_1_batch_is_quarantined_read_clean_then_removed_with_tag_and_catalog() -> (
    None
):
    batch = _batch()
    _quarantine_all(batch)
    # The build and the lab runtime start were read without every entity quarantined so far; nothing was deleted.
    assert set(batch.gates.seen_excluded[-1]) == ALL_PATHS
    assert LIVE not in batch.gates.seen_excluded[-1]
    assert batch.repository.files[ARCHIVED] >= ALL_PATHS

    batch.clock.advance(days=3)
    for entity in ENTITIES:
        assert (
            batch.handler.handle(_check(entity)).outcome
            is EnumQuarantineOutcome.STILL_QUARANTINED
        )
    refused = batch.handler.handle(_remove())
    assert refused.outcome is EnumQuarantineOutcome.REFUSED_CLOCK_RUNNING
    assert batch.repository.files[ARCHIVED] >= ALL_PATHS

    batch.clock.advance(days=4)
    for entity in ENTITIES:
        assert (
            batch.handler.handle(_check(entity)).outcome
            is EnumQuarantineOutcome.DUE_FOR_REMOVAL
        )
    removed = batch.handler.handle(_remove())
    assert removed.outcome is EnumQuarantineOutcome.REMOVED
    assert removed.removed_entities == ENTITIES
    # Entities and their tests are gone; the live file stays; one catalog row per entity under the one tag.
    assert batch.repository.files[ARCHIVED] == {LIVE}
    assert [
        (repo, row.entity, row.tag, row.sha) for repo, row in batch.store.catalog
    ] == [(ARCHIVED, e, TAG, SHA) for e in ENTITIES]


def test_stage_1_batch_red_gate_restores_one_entity_and_removes_nothing() -> None:
    batch = _batch()
    _quarantine_all(batch)
    batch.clock.advance(days=7)
    batch.gates.start = False
    restored = batch.handler.handle(_check("old_package"))
    assert restored.outcome is EnumQuarantineOutcome.RESTORED
    assert restored.failed_gate is EnumFailedGate.LAB_RUNTIME_START
    batch.gates.start = True

    # The restored entity is back in the build, so the batch that still names it removes nothing.
    refused = batch.handler.handle(_remove())
    assert refused.outcome is EnumQuarantineOutcome.REFUSED_NOT_QUARANTINED
    assert refused.entity == "old_package"
    assert batch.repository.files[ARCHIVED] >= ALL_PATHS
    assert batch.store.catalog == []

    # Its clock restarts from now: it is not due 7 days after the first quarantine, only 7 days after the new one.
    again = batch.handler.handle(_quarantine("old_package"))
    assert again.outcome is EnumQuarantineOutcome.QUARANTINED
    assert again.clock_started_at == batch.clock.now()
    batch.clock.advance(days=6)
    assert (
        batch.handler.handle(_remove()).outcome
        is EnumQuarantineOutcome.REFUSED_CLOCK_RUNNING
    )
    batch.clock.advance(days=1)
    assert batch.handler.handle(_remove()).outcome is EnumQuarantineOutcome.REMOVED


def test_stage_1_batch_without_a_catalog_row_for_every_entity_removes_nothing() -> None:
    batch = _batch()
    _quarantine_all(batch)
    batch.clock.advance(days=7)
    result = batch.handler.handle(_remove(skip_row_for="old_script"))
    assert result.outcome is EnumQuarantineOutcome.REFUSED_NO_CATALOG_ROW
    assert result.entity == "old_script"
    assert batch.repository.files[ARCHIVED] >= ALL_PATHS
    assert batch.store.catalog == []


def test_stage_1_batch_entity_with_a_public_consumer_never_enters_quarantine() -> None:
    batch = _batch()
    result = batch.handler.handle(_quarantine("old_module", consumers=1))
    assert result.outcome is EnumQuarantineOutcome.REFUSED_PUBLIC_CONSUMER
    assert result.excluded_paths == ()
    # With one entity never quarantined, a batch that names it is refused whole.
    for entity in ("old_package", "old_script"):
        batch.handler.handle(_quarantine(entity))
    batch.clock.advance(days=7)
    refused = batch.handler.handle(_remove())
    assert refused.outcome is EnumQuarantineOutcome.REFUSED_NOT_QUARANTINED
    assert refused.entity == "old_module"
    assert batch.repository.files[ARCHIVED] >= ALL_PATHS
