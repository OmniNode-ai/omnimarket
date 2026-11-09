# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The acceptance reads of OMN-23144 (AC1 to AC3), each on the in-memory rig and on the real files and git rig."""

from __future__ import annotations

import pytest

from omnimarket.nodes.node_code_quarantine_effect.models import (
    EnumFailedGate,
    EnumQuarantineOutcome,
)
from omnimarket.nodes.node_code_quarantine_effect.tests.support import (
    ENTITY,
    ENTITY_PATHS,
    KEPT,
    START,
    TEST_PATHS,
    Rig,
    check_request,
    quarantine_request,
    remove_request,
)

pytestmark = pytest.mark.unit


def test_quarantine_starts_clock(rig: Rig) -> None:
    result = rig.handler.handle(quarantine_request("ac1"))
    assert result.outcome is EnumQuarantineOutcome.QUARANTINED
    assert result.clock_started_at == START
    # Out of the build: the entity and its tests are in the excluded set; the kept file is not.
    assert set(result.excluded_paths) == {*ENTITY_PATHS, *TEST_PATHS}
    assert KEPT not in result.excluded_paths
    # Nothing deleted: every file is still in the repository.
    assert all(rig.exists(path) for path in (*ENTITY_PATHS, *TEST_PATHS, KEPT))


def test_quarantine_refuses_without_evidence_and_with_a_public_consumer(
    rig: Rig,
) -> None:
    short = rig.handler.handle(quarantine_request("ac1-e1", observation_days=3))
    assert short.outcome is EnumQuarantineOutcome.REFUSED_NO_EVIDENCE
    consumer = rig.handler.handle(quarantine_request("ac1-e2", consumers=1))
    assert consumer.outcome is EnumQuarantineOutcome.REFUSED_PUBLIC_CONSUMER
    assert short.excluded_paths == ()
    assert consumer.excluded_paths == ()
    again = rig.handler.handle(quarantine_request("ac1-e3"))
    assert again.outcome is EnumQuarantineOutcome.QUARANTINED
    twice = rig.handler.handle(quarantine_request("ac1-e4"))
    assert twice.outcome is EnumQuarantineOutcome.REFUSED_ALREADY_QUARANTINED
    assert twice.clock_started_at == START


@pytest.mark.parametrize(
    ("gate", "failed"),
    [
        ("build", EnumFailedGate.CLEAN_BUILD),
        ("start", EnumFailedGate.LAB_RUNTIME_START),
        ("reading", EnumFailedGate.MISSING_READING),
    ],
)
def test_failure_restores_and_resets(
    rig: Rig, gate: str, failed: EnumFailedGate
) -> None:
    assert (
        rig.handler.handle(quarantine_request("ac2")).outcome
        is EnumQuarantineOutcome.QUARANTINED
    )
    rig.clock.advance(days=5)
    rig.set_gate(gate, False)
    result = rig.handler.handle(check_request("ac2"))
    assert result.outcome is EnumQuarantineOutcome.RESTORED
    assert result.failed_gate is failed
    assert result.clock_started_at is None
    assert result.excluded_paths == ()
    assert all(rig.exists(path) for path in (*ENTITY_PATHS, *TEST_PATHS))
    # Quarantining again starts a fresh clock from now.
    rig.set_gate(gate, True)
    rig.clock.advance(days=1)
    fresh = rig.handler.handle(quarantine_request("ac2b"))
    assert fresh.outcome is EnumQuarantineOutcome.QUARANTINED
    assert fresh.clock_started_at == rig.clock.now()


@pytest.mark.parametrize(
    ("gate", "failed"),
    [
        ("build", EnumFailedGate.CLEAN_BUILD),
        ("start", EnumFailedGate.LAB_RUNTIME_START),
        ("reading", EnumFailedGate.MISSING_READING),
    ],
)
def test_failure_restores_and_resets_at_quarantine_time(
    rig: Rig, gate: str, failed: EnumFailedGate
) -> None:
    rig.set_gate(gate, False)
    result = rig.handler.handle(quarantine_request("ac2c"))
    assert result.outcome is EnumQuarantineOutcome.RESTORED
    assert result.failed_gate is failed
    assert result.clock_started_at is None
    assert result.excluded_paths == ()


def test_removal_needs_tag_and_catalog(rig: Rig) -> None:
    rig.handler.handle(quarantine_request("ac3"))
    rig.clock.advance(days=7)
    no_tag = rig.handler.handle(remove_request(rig, "ac3", tag="never-made"))
    assert no_tag.outcome is EnumQuarantineOutcome.REFUSED_NO_TAG
    no_row = rig.handler.handle(remove_request(rig, "ac3", with_row=False))
    assert no_row.outcome is EnumQuarantineOutcome.REFUSED_NO_CATALOG_ROW
    assert all(rig.exists(path) for path in (*ENTITY_PATHS, *TEST_PATHS))
    accepted = rig.handler.handle(remove_request(rig, "ac3"))
    assert accepted.outcome is EnumQuarantineOutcome.REMOVED
    assert accepted.removed_entities == (ENTITY,)
    assert not any(rig.exists(path) for path in (*ENTITY_PATHS, *TEST_PATHS))
    assert [r["entity"] for r in rig.catalog()] == [ENTITY]
