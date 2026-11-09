# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The golden chain and the error chain of the quarantine node, as functions over a rig.

The chain tests call them on the real handler; the mutation tests call them on a mutated handler and require
that they fail. Both read the typed events, the files and the catalog, never the handler's own state.
"""

from __future__ import annotations

from datetime import timedelta

from omnimarket.nodes.node_code_quarantine_effect.models import (
    EnumFailedGate,
    EnumQuarantineOutcome,
    ModelQuarantineEvent,
)
from omnimarket.nodes.node_code_quarantine_effect.tests.support import (
    ENTITY,
    ENTITY_PATHS,
    KEPT,
    REPO,
    START,
    TAG,
    TEST_PATHS,
    Rig,
    check_request,
    quarantine_request,
    remove_request,
)

RUN = "chain1"


def kinds(rig: Rig) -> list[str]:
    return [e.kind for e in rig.events() if isinstance(e, ModelQuarantineEvent)]


def golden_chain(rig: Rig) -> None:
    """Quarantine, a green check on day 3, a due check on day 7, a removal with a tag and a catalog row."""
    first = rig.handler.handle(quarantine_request(RUN))
    assert first.outcome is EnumQuarantineOutcome.QUARANTINED
    assert first.clock_started_at == START
    assert set(first.excluded_paths) == {*ENTITY_PATHS, *TEST_PATHS}
    assert all(rig.exists(path) for path in (*ENTITY_PATHS, *TEST_PATHS, KEPT))

    rig.clock.advance(days=3)
    mid = rig.handler.handle(check_request(RUN))
    assert mid.outcome is EnumQuarantineOutcome.STILL_QUARANTINED
    assert mid.clock_started_at == START

    rig.clock.advance(days=4)
    due = rig.handler.handle(check_request(RUN))
    assert due.outcome is EnumQuarantineOutcome.DUE_FOR_REMOVAL

    done = rig.handler.handle(remove_request(rig, RUN))
    assert done.outcome is EnumQuarantineOutcome.REMOVED
    assert done.removed_entities == (ENTITY,)
    assert not any(rig.exists(path) for path in (*ENTITY_PATHS, *TEST_PATHS))
    assert rig.exists(KEPT)
    assert done.excluded_paths == ()
    rows = rig.catalog()
    assert [(r["repository"], r["entity"], r["tag"], r["sha"]) for r in rows] == [
        (REPO, ENTITY, TAG, rig.tag_sha)
    ]

    assert kinds(rig) == [
        "code-quarantine.quarantine.quarantined",
        "code-quarantine.check.still-quarantined",
        "code-quarantine.check.due-for-removal",
        "code-quarantine.remove.removed",
    ]
    events = [e for e in rig.events() if isinstance(e, ModelQuarantineEvent)]
    assert {e.correlation_id for e in events} == {RUN}
    assert {e.reading for e in events} == {"WORKING"}


def _quarantined(rig: Rig, run_id: str = RUN) -> None:
    assert (
        rig.handler.handle(quarantine_request(run_id)).outcome
        is EnumQuarantineOutcome.QUARANTINED
    )


def error_chain(rig: Rig) -> None:
    """Each fault ends in a typed failure event that reads NOT WORKING, and a restore resets the clock."""
    # A red gate restores the entity and resets the clock, whichever gate it is.
    for gate, failed in (
        ("build", EnumFailedGate.CLEAN_BUILD),
        ("start", EnumFailedGate.LAB_RUNTIME_START),
        ("reading", EnumFailedGate.MISSING_READING),
    ):
        _quarantined(rig)
        rig.clock.advance(days=2)
        rig.set_gate(gate, False)
        restored = rig.handler.handle(check_request(RUN))
        assert restored.outcome is EnumQuarantineOutcome.RESTORED
        assert restored.failed_gate is failed
        assert restored.clock_started_at is None
        assert restored.excluded_paths == ()
        rig.set_gate(gate, True)
    # Restored means out of quarantine: a check now refuses, and a removal is refused as not quarantined.
    assert (
        rig.handler.handle(check_request(RUN)).outcome
        is EnumQuarantineOutcome.REFUSED_NOT_QUARANTINED
    )
    assert (
        rig.handler.handle(remove_request(rig, RUN)).outcome
        is EnumQuarantineOutcome.REFUSED_NOT_QUARANTINED
    )
    # The clock restarted at the new quarantine, not at the first one.
    restarted_at = rig.clock.now()
    _quarantined(rig)
    rig.clock.advance(days=6)
    early = rig.handler.handle(remove_request(rig, RUN))
    assert early.outcome is EnumQuarantineOutcome.REFUSED_CLOCK_RUNNING
    assert early.clock_started_at == restarted_at
    assert all(rig.exists(path) for path in (*ENTITY_PATHS, *TEST_PATHS))
    rig.clock.advance(days=1)
    # A removal with no tag, with a tag the repository does not have, or with no catalog row is refused.
    no_tag = rig.handler.handle(remove_request(rig, RUN, tag=""))
    assert no_tag.outcome is EnumQuarantineOutcome.REFUSED_NO_TAG
    missing_tag = rig.handler.handle(remove_request(rig, RUN, tag="never-made"))
    assert missing_tag.outcome is EnumQuarantineOutcome.REFUSED_NO_TAG
    no_row = rig.handler.handle(remove_request(rig, RUN, with_row=False))
    assert no_row.outcome is EnumQuarantineOutcome.REFUSED_NO_CATALOG_ROW
    wrong_sha = rig.handler.handle(remove_request(rig, RUN, row_sha="deadbeef"))
    assert wrong_sha.outcome is EnumQuarantineOutcome.REFUSED_NO_CATALOG_ROW
    assert all(rig.exists(path) for path in (*ENTITY_PATHS, *TEST_PATHS))
    assert rig.catalog() == []
    # A seam that is down is a typed failure, not silence.
    rig.break_store()
    down = rig.handler.handle(check_request(RUN))
    assert down.outcome is EnumQuarantineOutcome.BACKEND_FAILED

    events = [e for e in rig.events() if isinstance(e, ModelQuarantineEvent)]
    assert events
    assert {e.correlation_id for e in events} == {RUN}
    failures = [e for e in events if e.reading == "NOT WORKING"]
    refused = {
        e.outcome
        for e in failures
        if e.outcome.value.startswith("refused-") or e.outcome.value == "backend-failed"
    }
    assert {
        EnumQuarantineOutcome.REFUSED_NO_TAG,
        EnumQuarantineOutcome.REFUSED_NO_CATALOG_ROW,
        EnumQuarantineOutcome.REFUSED_CLOCK_RUNNING,
        EnumQuarantineOutcome.BACKEND_FAILED,
    } <= refused
    restored_events = [e for e in events if e.outcome is EnumQuarantineOutcome.RESTORED]
    assert len(restored_events) == 3
    assert {e.reading for e in restored_events} == {"NOT WORKING"}
    assert timedelta(days=7) == rig.clock.now() - restarted_at
