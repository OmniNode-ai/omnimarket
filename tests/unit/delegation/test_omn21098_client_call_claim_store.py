# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The client-call claim store keeps the rules of the AgentCallAdapter model (OMN-21098, TB.3).

Each invariant is a check that the real store passes and that one named
injected break fails. The breaks are the in-process counterparts of the TLA+
mutants in ``formal/agent_call_adapter``: M1 no generation fencing, M3 a blind
upsert that resets a live row, M4 a takeover that resumes before the recorded
dispatch intent. Two more breaks cover the conflict and the live-lease rules.
A check that passes on its break would be a check that cannot fail.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from omnimarket.nodes.node_delegate_skill_orchestrator.models.enum_client_call_claim_action import (
    EnumClientCallClaimAction as Action,
)
from omnimarket.nodes.node_delegate_skill_orchestrator.models.enum_client_call_claim_action import (
    EnumClientCallResumeStep as Resume,
)
from omnimarket.nodes.node_delegate_skill_orchestrator.models.enum_client_call_claim_state import (
    EnumClientCallClaimState as State,
)
from omnimarket.nodes.node_delegate_skill_orchestrator.models.model_client_call_claim_decision import (
    ModelClientCallClaimDecision,
)
from omnimarket.nodes.node_delegate_skill_orchestrator.models.model_client_call_claim_row import (
    ModelClientCallClaimRow,
)
from omnimarket.nodes.node_delegate_skill_orchestrator.ports.port_client_call_claim import (
    InMemoryClientCallClaimStore,
    ProtocolClientCallClaimStore,
)

pytestmark = pytest.mark.unit

KEY = "a" * 64
FP = "b" * 64
OTHER_FP = "c" * 64
LEASE_SECONDS = 30.0


class _Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 11, 7, 12, 0, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


# -- the named injected breaks ------------------------------------------------


class _M1NoFencing(InMemoryClientCallClaimStore):
    def _writable(self, row: ModelClientCallClaimRow, generation: int) -> bool:
        return True


class _M3BlindUpsert(InMemoryClientCallClaimStore):
    def _claim_existing(
        self,
        row: ModelClientCallClaimRow,
        request_fingerprint: str,
        owner_id: str,
        now: datetime,
    ) -> ModelClientCallClaimDecision:
        reset = row.model_copy(
            update={
                "request_fingerprint": request_fingerprint,
                "state": State.CLAIMED,
                "generation": row.generation + 1,
                "owner_id": owner_id,
                "lease_expires_at": now + timedelta(seconds=LEASE_SECONDS),
            }
        )
        self._rows[row.client_call_key] = reset
        return ModelClientCallClaimDecision(action=Action.WON, row=reset)


class _M4ResumeBeforeIntent(InMemoryClientCallClaimStore):
    def _resume_step(self, state: State) -> Resume:
        if state is State.DISPATCH_INTENDED:
            return Resume.PUBLISH_ADMISSION
        return super()._resume_step(state)


class _ConflictIgnored(InMemoryClientCallClaimStore):
    def _claim_existing(
        self,
        row: ModelClientCallClaimRow,
        request_fingerprint: str,
        owner_id: str,
        now: datetime,
    ) -> ModelClientCallClaimDecision:
        return super()._claim_existing(row, row.request_fingerprint, owner_id, now)


class _LiveLeaseIgnored(InMemoryClientCallClaimStore):
    def _claim_existing(
        self,
        row: ModelClientCallClaimRow,
        request_fingerprint: str,
        owner_id: str,
        now: datetime,
    ) -> ModelClientCallClaimDecision:
        expired = row.model_copy(update={"lease_expires_at": now})
        return super()._claim_existing(expired, request_fingerprint, owner_id, now)


StoreFactory = Callable[[Callable[[], datetime]], InMemoryClientCallClaimStore]


def _real(clock: Callable[[], datetime]) -> InMemoryClientCallClaimStore:
    return InMemoryClientCallClaimStore(clock=clock, lease_seconds=LEASE_SECONDS)


def _build(cls: type[InMemoryClientCallClaimStore]) -> StoreFactory:
    return lambda clock: cls(clock=clock, lease_seconds=LEASE_SECONDS)


# -- the invariants, each a check over a store factory --------------------------


def _check_one_winner_under_concurrency(factory: StoreFactory) -> None:
    store = factory(_Clock())
    workers = 16
    barrier = threading.Barrier(workers)
    actions: list[Action] = []

    def run(i: int) -> None:
        barrier.wait()
        decision = store.claim(
            client_call_key=KEY, request_fingerprint=FP, owner_id=f"owner-{i}"
        )
        actions.append(decision.action)

    threads = [threading.Thread(target=run, args=(i,)) for i in range(workers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert actions.count(Action.WON) == 1
    assert actions.count(Action.WAIT_LIVE_OWNER) == workers - 1
    row = store.read(KEY)
    assert row is not None
    assert row.generation == 1


def _check_stale_owner_write_changes_nothing(factory: StoreFactory) -> None:
    clock = _Clock()
    store = factory(clock)
    first = store.claim(client_call_key=KEY, request_fingerprint=FP, owner_id="a")
    assert first.action is Action.WON
    assert first.row.generation == 1
    clock.advance(LEASE_SECONDS + 1)
    second = store.claim(client_call_key=KEY, request_fingerprint=FP, owner_id="b")
    assert second.action is Action.TAKEOVER
    assert second.row.generation == 2
    before = store.read(KEY)
    changed = store.advance(
        client_call_key=KEY,
        generation=1,
        to_state=State.ADMITTED,
        effective_mode="selective",
        admitted=True,
    )
    assert changed is False
    assert store.read(KEY) == before


def _check_conflict_writes_nothing(factory: StoreFactory) -> None:
    clock = _Clock()
    store = factory(clock)
    store.claim(client_call_key=KEY, request_fingerprint=FP, owner_id="a")
    before = store.read(KEY)
    decision = store.claim(
        client_call_key=KEY, request_fingerprint=OTHER_FP, owner_id="b"
    )
    assert decision.action is Action.CONFLICT
    assert store.read(KEY) == before
    clock.advance(LEASE_SECONDS + 1)
    expired = store.claim(
        client_call_key=KEY, request_fingerprint=OTHER_FP, owner_id="c"
    )
    assert expired.action is Action.CONFLICT
    assert store.read(KEY) == before


def _check_live_lease_waits(factory: StoreFactory) -> None:
    clock = _Clock()
    store = factory(clock)
    store.claim(client_call_key=KEY, request_fingerprint=FP, owner_id="a")
    clock.advance(LEASE_SECONDS - 1)
    decision = store.claim(client_call_key=KEY, request_fingerprint=FP, owner_id="b")
    assert decision.action is Action.WAIT_LIVE_OWNER
    row = store.read(KEY)
    assert row is not None
    assert row.generation == 1
    assert row.owner_id == "a"


def _check_recorded_intent_is_never_redispatched(factory: StoreFactory) -> None:
    clock = _Clock()
    store = factory(clock)
    store.claim(client_call_key=KEY, request_fingerprint=FP, owner_id="a")
    assert store.advance(
        client_call_key=KEY,
        generation=1,
        to_state=State.ADMITTED,
        effective_mode="selective",
        admitted=True,
    )
    assert store.advance(
        client_call_key=KEY, generation=1, to_state=State.DISPATCH_INTENDED
    )
    clock.advance(LEASE_SECONDS + 1)
    decision = store.claim(client_call_key=KEY, request_fingerprint=FP, owner_id="b")
    assert decision.action is Action.TAKEOVER
    assert decision.resume_step is Resume.AWAIT_TERMINAL


_INVARIANTS: dict[
    str, tuple[Callable[[StoreFactory], None], type[InMemoryClientCallClaimStore]]
] = {
    "one_winner_under_concurrency__M3_blind_upsert": (
        _check_one_winner_under_concurrency,
        _M3BlindUpsert,
    ),
    "stale_owner_write_changes_nothing__M1_no_fencing": (
        _check_stale_owner_write_changes_nothing,
        _M1NoFencing,
    ),
    "conflict_writes_nothing__conflict_ignored": (
        _check_conflict_writes_nothing,
        _ConflictIgnored,
    ),
    "live_lease_waits__live_lease_ignored": (
        _check_live_lease_waits,
        _LiveLeaseIgnored,
    ),
    "recorded_intent_never_redispatched__M4_resume_before_intent": (
        _check_recorded_intent_is_never_redispatched,
        _M4ResumeBeforeIntent,
    ),
}


@pytest.mark.parametrize("name", sorted(_INVARIANTS))
def test_the_real_store_keeps_the_invariant(name: str) -> None:
    check, _ = _INVARIANTS[name]
    check(_real)


@pytest.mark.parametrize("name", sorted(_INVARIANTS))
def test_the_named_break_fails_the_same_check(name: str) -> None:
    check, mutant = _INVARIANTS[name]
    with pytest.raises(AssertionError):
        check(_build(mutant))


# -- the rest of the store's behaviour ----------------------------------------


def test_a_stored_terminal_is_republished_to_a_later_command() -> None:
    store = _real(_Clock())
    store.claim(client_call_key=KEY, request_fingerprint=FP, owner_id="a")
    assert store.advance(
        client_call_key=KEY,
        generation=1,
        to_state=State.OBSERVED,
        effective_mode="observe",
        admitted=False,
    )
    assert store.advance(
        client_call_key=KEY,
        generation=1,
        to_state=State.TERMINAL,
        terminal_json='{"status":"observed"}',
    )
    again = store.claim(client_call_key=KEY, request_fingerprint=FP, owner_id="b")
    assert again.action is Action.REPUBLISH_TERMINAL
    assert again.row.terminal_json == '{"status":"observed"}'
    assert again.row.generation == 1


def test_a_takeover_resumes_from_the_recorded_state() -> None:
    clock = _Clock()
    store = _real(clock)
    store.claim(client_call_key=KEY, request_fingerprint=FP, owner_id="a")
    clock.advance(LEASE_SECONDS + 1)
    claimed = store.claim(client_call_key=KEY, request_fingerprint=FP, owner_id="b")
    assert claimed.resume_step is Resume.ADMIT
    assert store.advance(
        client_call_key=KEY,
        generation=2,
        to_state=State.ADMITTED,
        effective_mode="selective",
        admitted=True,
    )
    clock.advance(LEASE_SECONDS + 1)
    admitted = store.claim(client_call_key=KEY, request_fingerprint=FP, owner_id="c")
    assert admitted.resume_step is Resume.PUBLISH_ADMISSION
    assert admitted.row.generation == 3
    assert admitted.row.effective_mode == "selective"


def test_the_generation_only_rises_across_takeovers() -> None:
    clock = _Clock()
    store = _real(clock)
    generations = [
        store.claim(
            client_call_key=KEY, request_fingerprint=FP, owner_id="o0"
        ).row.generation
    ]
    for i in range(1, 4):
        clock.advance(LEASE_SECONDS + 1)
        generations.append(
            store.claim(
                client_call_key=KEY, request_fingerprint=FP, owner_id=f"o{i}"
            ).row.generation
        )
    assert generations == [1, 2, 3, 4]


@pytest.mark.parametrize(
    ("first", "to_state"),
    [
        (State.CLAIMED, State.TERMINAL),
        (State.CLAIMED, State.DISPATCH_INTENDED),
        (State.NOT_ADMITTED, State.DISPATCH_INTENDED),
        (State.OBSERVED, State.DISPATCH_INTENDED),
    ],
)
def test_a_transition_the_model_does_not_have_changes_nothing(
    first: State, to_state: State
) -> None:
    store = _real(_Clock())
    store.claim(client_call_key=KEY, request_fingerprint=FP, owner_id="a")
    if first is not State.CLAIMED:
        assert store.advance(
            client_call_key=KEY, generation=1, to_state=first, admitted=False
        )
    before = store.read(KEY)
    assert store.advance(client_call_key=KEY, generation=1, to_state=to_state) is False
    assert store.read(KEY) == before


def test_a_write_for_an_absent_key_changes_nothing() -> None:
    store = _real(_Clock())
    assert (
        store.advance(client_call_key=KEY, generation=1, to_state=State.ADMITTED)
        is False
    )
    assert store.read(KEY) is None


def test_stalled_lists_only_expired_non_terminal_rows() -> None:
    clock = _Clock()
    store = _real(clock)
    store.claim(client_call_key=KEY, request_fingerprint=FP, owner_id="a")
    other = "d" * 64
    store.claim(client_call_key=other, request_fingerprint=FP, owner_id="a")
    assert store.stalled() == []
    clock.advance(LEASE_SECONDS + 1)
    assert {r.client_call_key for r in store.stalled()} == {KEY, other}
    assert store.advance(
        client_call_key=other,
        generation=1,
        to_state=State.OBSERVED,
        effective_mode="observe",
        admitted=False,
    )
    assert store.advance(
        client_call_key=other, generation=1, to_state=State.TERMINAL, terminal_json="{}"
    )
    assert {r.client_call_key for r in store.stalled()} == {KEY}


def test_the_store_satisfies_its_protocol_and_refuses_a_non_positive_lease() -> None:
    assert isinstance(_real(_Clock()), ProtocolClientCallClaimStore)
    with pytest.raises(ValueError, match="lease_seconds"):
        InMemoryClientCallClaimStore(clock=_Clock(), lease_seconds=0)


def test_a_row_refuses_a_key_that_is_not_a_sha256() -> None:
    with pytest.raises(ValidationError):
        ModelClientCallClaimRow(
            client_call_key="not-a-hash",
            request_fingerprint=FP,
            state=State.CLAIMED,
            generation=1,
            owner_id="a",
            lease_expires_at=datetime(2026, 11, 7, tzinfo=UTC),
        )
