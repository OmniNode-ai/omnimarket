# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The in-memory twin passes the frozen contract suite; named breaks fail it for real."""

from __future__ import annotations

import copy
import hashlib
import time
from collections.abc import Callable
from pathlib import Path

import pytest

from omnimarket.models.model_gate_store import (
    ModelGateStorePolicy,
    ModelGateStoreRequest,
    ModelGateStoreResult,
)
from omnimarket.nodes.node_gate_store_effect.handlers.store_in_memory import (
    STAGES,
    InMemoryGateStore,
    _State,
)
from omnimarket.protocols.protocol_gate_store import ProtocolGateStore
from tests.nodes.node_gate_store_effect import contract_suite as suite

pytestmark = pytest.mark.unit

HERE = Path(__file__).parent


def twin(
    policy: ModelGateStorePolicy, hook: Callable[[str], None] | None
) -> ProtocolGateStore:
    return InMemoryGateStore(policy, hook)


class NonAtomicStore(InMemoryGateStore):
    """Break ``non_atomic_reserve``: decide on a snapshot outside the lock, then overwrite."""

    def transact(self, request: ModelGateStoreRequest) -> ModelGateStoreResult:
        work = copy.deepcopy(self._state)
        result = self._apply(work, request)
        time.sleep(0.02)
        self._state = work
        return result


class UnfencedStore(InMemoryGateStore):
    """Break ``handover_without_fence``: the handover ignores the epoch."""

    def _handover(
        self, state: _State, request: ModelGateStoreRequest
    ) -> ModelGateStoreResult:
        state.holder = request.holder
        return self._write(state, request)


class PartialCommitStore(InMemoryGateStore):
    """Break ``commit_before_kill_point``: the state is replaced before the last stage."""

    def transact(self, request: ModelGateStoreRequest) -> ModelGateStoreResult:
        with self._lock:
            self._at("before-apply")
            work = copy.deepcopy(self._state)
            result = self._apply(work, request)
            self._at("after-apply")
            self._state = work
            self._at("before-commit")
            return result


class FreeOnReleaseStore(InMemoryGateStore):
    """Break ``free_consumed_on_terminal``: a release frees a consumed reservation."""

    def _release(
        self, state: _State, request: ModelGateStoreRequest
    ) -> ModelGateStoreResult:
        return super()._release(
            state, request.model_copy(update={"process_gone": True})
        )


class NoStepKeyMemoStore(InMemoryGateStore):
    """Break ``forget_step_keys``: the recorded answers are dropped before each request."""

    def transact(self, request: ModelGateStoreRequest) -> ModelGateStoreResult:
        self._state.steps.clear()
        return super().transact(request)


class UnstoppedStore(InMemoryGateStore):
    """Break ``safety_after_count``: a recorded stop is not consulted by a reservation."""

    def _reserve(
        self, state: _State, request: ModelGateStoreRequest
    ) -> ModelGateStoreResult:
        state.safety.clear()
        return super()._reserve(state, request)


@pytest.mark.parametrize("check", suite.CHECKS, ids=lambda c: c.__name__)
def test_twin_passes_contract_suite(check: Callable[[suite.Factory], None]) -> None:
    check(twin)


def test_stages_match_the_suite() -> None:
    assert STAGES == suite.KILL_STAGES


BREAKS: list[tuple[str, type[InMemoryGateStore], Callable[[suite.Factory], None]]] = [
    ("non_atomic_reserve", NonAtomicStore, suite.check_nine_racing_reservations),
    ("handover_without_fence", UnfencedStore, suite.check_handover_is_fenced),
    ("commit_before_kill_point", PartialCommitStore, suite.check_kill_at_each_stage),
    (
        "free_consumed_on_terminal",
        FreeOnReleaseStore,
        suite.check_consumed_counts_until_process_gone,
    ),
    (
        "forget_step_keys",
        NoStepKeyMemoStore,
        suite.check_duplicate_delivery_and_replayed_keys,
    ),
    (
        "safety_after_count",
        UnstoppedStore,
        suite.check_safety_write_commits_before_it_counts,
    ),
]


@pytest.mark.parametrize(
    ("name", "store_type", "check"), BREAKS, ids=[b[0] for b in BREAKS]
)
def test_named_break_fails_the_suite(
    name: str,
    store_type: type[InMemoryGateStore],
    check: Callable[[suite.Factory], None],
) -> None:
    def broken(
        policy: ModelGateStorePolicy, hook: Callable[[str], None] | None
    ) -> ProtocolGateStore:
        return store_type(policy, hook)

    with pytest.raises(AssertionError):
        check(broken)


def test_contract_suite_frozen() -> None:
    """The suite is frozen with a hash; changing it needs the hash file changed with it."""
    frozen = (HERE / "contract_suite.sha256").read_text(encoding="utf-8").strip()
    actual = hashlib.sha256((HERE / "contract_suite.py").read_bytes()).hexdigest()
    assert actual == frozen, f"contract_suite.py is {actual}; frozen {frozen}"
