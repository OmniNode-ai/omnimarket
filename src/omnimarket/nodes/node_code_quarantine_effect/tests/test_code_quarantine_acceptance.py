# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The acceptance reads of OMN-23144 (AC1 to AC3) and OMN-23149 (stage 4, AC1 and AC2), each on the in-memory rig and on the real files and git rig."""

from __future__ import annotations

import pytest

from omnimarket.nodes.node_code_quarantine_effect.models import (
    EnumFailedGate,
    EnumQuarantineOutcome,
    ModelRarePathCheck,
)
from omnimarket.nodes.node_code_quarantine_effect.tests.support import (
    ENTITY,
    ENTITY_PATHS,
    KEPT,
    SKILL,
    SKILL_PATHS,
    START,
    TEST_PATHS,
    Rig,
    check_request,
    quarantine_request,
    remove_request,
    stage_four_module_request,
    stage_four_skill_request,
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


def test_stage_4_tests_go_with_module(rig: Rig) -> None:
    """OMN-23149 AC1: a module referenced only by tests leaves with those tests, never the tests alone."""
    everything = (*ENTITY_PATHS, *TEST_PATHS)
    # The tests alone are refused, and so is a module whose referencing tests stay behind.
    alone = rig.handler.handle(
        stage_four_module_request("s4a", entity_paths=TEST_PATHS)
    )
    assert alone.outcome is EnumQuarantineOutcome.REFUSED_TESTS_NOT_WITH_MODULE
    left = rig.handler.handle(stage_four_module_request("s4b", test_paths=()))
    assert left.outcome is EnumQuarantineOutcome.REFUSED_TESTS_NOT_WITH_MODULE
    unread = rig.handler.handle(stage_four_module_request("s4c", test_references=()))
    assert unread.outcome is EnumQuarantineOutcome.REFUSED_TESTS_NOT_WITH_MODULE
    for refused in (alone, left, unread):
        assert refused.excluded_paths == ()
        assert refused.clock_started_at is None
        assert not refused.changed
    assert all(rig.exists(path) for path in everything)
    # The module and its tests together are quarantined, and removed together once the clock has run.
    started = rig.handler.handle(stage_four_module_request("s4d"))
    assert started.outcome is EnumQuarantineOutcome.QUARANTINED
    assert set(started.excluded_paths) == set(everything)
    rig.clock.advance(days=7)
    removed = rig.handler.handle(remove_request(rig, "s4d"))
    assert removed.outcome is EnumQuarantineOutcome.REMOVED
    assert not any(rig.exists(path) for path in everything)
    assert rig.exists(KEPT)


def test_stage_4_skill_rare_path(rig: Rig) -> None:
    """OMN-23149 AC2: a skill never seen goes only after a rare-path check names no drill that uses it."""
    missing = rig.handler.handle(
        stage_four_skill_request("s4e").model_copy(update={"rare_path": None})
    )
    assert missing.outcome is EnumQuarantineOutcome.REFUSED_RARE_PATH
    unread = rig.handler.handle(
        stage_four_skill_request(
            "s4f",
            rare_path=ModelRarePathCheck(
                evidence_ref="reports/rare-path/skills.md", drills_checked=()
            ),
        )
    )
    assert unread.outcome is EnumQuarantineOutcome.REFUSED_RARE_PATH
    used = rig.handler.handle(
        stage_four_skill_request(
            "s4g",
            rare_path=ModelRarePathCheck(
                evidence_ref="reports/rare-path/skills.md",
                drills_checked=("drill-failover", "drill-restore"),
                drills_using=("drill-restore",),
            ),
        )
    )
    assert used.outcome is EnumQuarantineOutcome.REFUSED_RARE_PATH
    assert "drill-restore" in used.detail
    for refused in (missing, unread, used):
        assert refused.excluded_paths == ()
        assert not refused.changed
    assert all(rig.exists(path) for path in SKILL_PATHS)
    # With a check that read the drills and found none using the skill, it is quarantined, then removed.
    started = rig.handler.handle(stage_four_skill_request("s4h"))
    assert started.outcome is EnumQuarantineOutcome.QUARANTINED
    assert set(started.excluded_paths) == set(SKILL_PATHS)
    rig.clock.advance(days=7)
    removed = rig.handler.handle(remove_request(rig, "s4h", entity=SKILL))
    assert removed.outcome is EnumQuarantineOutcome.REMOVED
    assert not any(rig.exists(path) for path in SKILL_PATHS)
