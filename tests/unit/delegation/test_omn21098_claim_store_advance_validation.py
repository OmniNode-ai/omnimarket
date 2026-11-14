# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""advance() validates each write against the state it enters (OMN-21098, TB.3).

Three review findings on the claim store, each pinned by a test that failed on
the store before the check existed:

* a TERMINAL write with an empty ``terminal_json`` leaves a row nothing can replay;
* ADMITTED can be entered without a recorded admission, or with ``admitted=False``;
* a later DISPATCH_INTENDED or TERMINAL write overwrites the committed admission.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from omnimarket.nodes.node_delegate_skill_orchestrator.models.enum_client_call_claim_state import (
    EnumClientCallClaimState as State,
)
from omnimarket.nodes.node_delegate_skill_orchestrator.ports.port_client_call_claim import (
    InMemoryCallClaimStore,
)

pytestmark = pytest.mark.unit

KEY = "a" * 64
FP = "b" * 64


def _store() -> InMemoryCallClaimStore:
    return InMemoryCallClaimStore(
        clock=lambda: datetime(2026, 11, 7, 12, 0, 0, tzinfo=UTC), lease_seconds=30.0
    )


def _claimed() -> InMemoryCallClaimStore:
    store = _store()
    store.claim(client_call_key=KEY, request_fingerprint=FP, owner_id="a")
    return store


def _admitted() -> InMemoryCallClaimStore:
    store = _claimed()
    assert store.advance(
        client_call_key=KEY,
        generation=1,
        to_state=State.ADMITTED,
        effective_mode="selective",
        admitted=True,
    )
    return store


def test_terminal_with_empty_terminal_json_changes_nothing() -> None:
    store = _claimed()
    assert store.advance(
        client_call_key=KEY,
        generation=1,
        to_state=State.OBSERVED,
        effective_mode="observe",
        admitted=False,
    )
    before = store.read(KEY)
    assert (
        store.advance(client_call_key=KEY, generation=1, to_state=State.TERMINAL)
        is False
    )
    assert store.read(KEY) == before


@pytest.mark.parametrize(
    ("effective_mode", "admitted"),
    [(None, None), ("selective", None), (None, True), ("selective", False)],
)
def test_admitted_needs_a_recorded_mode_and_admitted_true(
    effective_mode: str | None, admitted: bool | None
) -> None:
    store = _claimed()
    before = store.read(KEY)
    assert (
        store.advance(
            client_call_key=KEY,
            generation=1,
            to_state=State.ADMITTED,
            effective_mode=effective_mode,
            admitted=admitted,
        )
        is False
    )
    assert store.read(KEY) == before


@pytest.mark.parametrize("to_state", [State.NOT_ADMITTED, State.OBSERVED])
def test_a_non_admission_must_record_admitted_false(to_state: State) -> None:
    store = _claimed()
    before = store.read(KEY)
    assert (
        store.advance(
            client_call_key=KEY, generation=1, to_state=to_state, admitted=True
        )
        is False
    )
    assert store.read(KEY) == before


def test_dispatch_intended_does_not_overwrite_the_committed_admission() -> None:
    store = _admitted()
    before = store.read(KEY)
    changed = store.advance(
        client_call_key=KEY,
        generation=1,
        to_state=State.DISPATCH_INTENDED,
        effective_mode="observe",
        admitted=False,
    )
    row = store.read(KEY)
    assert row is not None
    assert before is not None
    assert row.effective_mode == before.effective_mode == "selective"
    assert row.admitted is True
    assert changed is False


def test_terminal_does_not_overwrite_the_committed_admission() -> None:
    store = _admitted()
    assert store.advance(
        client_call_key=KEY, generation=1, to_state=State.DISPATCH_INTENDED
    )
    changed = store.advance(
        client_call_key=KEY,
        generation=1,
        to_state=State.TERMINAL,
        effective_mode="observe",
        admitted=False,
        terminal_json='{"status":"done"}',
    )
    row = store.read(KEY)
    assert row is not None
    assert row.effective_mode == "selective"
    assert row.admitted is True
    assert changed is False


def test_a_valid_admission_and_terminal_still_land() -> None:
    store = _admitted()
    assert store.advance(
        client_call_key=KEY, generation=1, to_state=State.DISPATCH_INTENDED
    )
    assert store.advance(
        client_call_key=KEY,
        generation=1,
        to_state=State.TERMINAL,
        terminal_json='{"status":"done"}',
    )
    row = store.read(KEY)
    assert row is not None
    assert row.state is State.TERMINAL
