# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-16924 — the reducer's state of record is the DATABASE, not a local file.

THE DEFECT. ``handler_session_phase_reducer.py:29`` declared
``_DEFAULT_STATE_PATH = ".onex_state/session/phase_state.yaml"`` — relative to
the process cwd — and ``handle()`` took it as a keyword default, so every bus
dispatch used it. The runtime container's cwd is ``/app``, owned ``root:root``
mode ``0755``, and the runtime process runs as ``omniinfra``::

    docker exec -u omniinfra omninode-stability-test-runtime \\
      sh -c "cd /app && touch .onex_state_probe"
      -> touch: cannot touch '.onex_state_probe': Permission denied

so ``_write_state``'s ``path.parent.mkdir(...)`` could never succeed::

    [ERROR] omnibase_infra.runtime.service_kernel: Dispatcher
      'dispatcher.auto.node_session_phase_reducer.HandlerSessionPhaseReducer.
       reduce_session_phase_f81977b1' failed:
      PermissionError: [Errno 13] Permission denied: '.onex_state'
    [ERROR] handler_wiring: metric_name=boundary_swallow_prevented dlq_routed=true

4,063 ``boundary_swallow_prevented`` in ~5 minutes (~13.5/s) on the stability
lane; a 100% failure rate on all three subscribed topics, latent identically on
every lane (dev showed zero only because it had no traffic).

Operator ruling 2026-08-29, verbatim: *"onex_state should be configurable via
contract overlay right? for our purposes, state should only be kept in the
database. if you disagree, let's have a conversation."*

RED-FIRST. ``test_fold_under_an_unwritable_cwd_does_not_raise`` reproduces the
750/750 permission shape directly: a cwd the process cannot write. Against
pre-fix ``dev`` it raises ``PermissionError: [Errno 13] Permission denied:
'.onex_state'`` — the live failure, in a hermetic test. The remaining tests are
red for a second reason: pre-fix, prior state came from a file, so binding a
durable row changed nothing and nothing was ever flushed back for the runtime to
persist.

The paired overlay red case — an overlay rebinding the durable table with no
code change — lives on the runtime side of the seam, in
``omnibase_infra/tests/integration/test_state_io_domain_key_omn16924.py``,
because it is the runtime that reads ``state_io`` and builds the adapter.
"""

from __future__ import annotations

import os
import stat
from datetime import UTC, datetime
from pathlib import Path

import pytest

from omnimarket.nodes.node_session_phase_reducer.handlers.handler_session_phase_reducer import (
    HandlerSessionPhaseReducer,
    ModelSessionPhaseReducerInput,
    ModelSessionPhaseState,
)
from omnimarket.nodes.node_session_phase_reducer.state_codec import (
    decode,
    encode,
    get_default_proxy,
    reset_default_proxy,
)
from tests.session_phase_state_io_harness import StateIoRowStore, state_io_dispatch

_SESSION = "omn16162-live-proof-1787064555"
_T0 = datetime(2026, 8, 29, 7, 7, 1, tzinfo=UTC)
_T1 = datetime(2026, 8, 29, 7, 22, 1, tzinfo=UTC)


def _started() -> ModelSessionPhaseReducerInput:
    return ModelSessionPhaseReducerInput(
        event_type="session.started",
        session_id=_SESSION,
        timestamp=_T0,
        phase="health_gate",
        budget_elapsed_pct=10,
        active_worker_count=2,
    )


def _phase_advance() -> ModelSessionPhaseReducerInput:
    return ModelSessionPhaseReducerInput(
        event_type="session.phase.state",
        session_id=_SESSION,
        timestamp=_T1,
        phase="merge",
        phase_index=3,
        budget_elapsed_pct=55,
    )


@pytest.mark.integration
def test_fold_under_an_unwritable_cwd_does_not_raise(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The live 750/750 failure shape, hermetically. RED against pre-fix dev.

    ``/app`` in the runtime container is ``root:root 0755`` and the process runs
    as ``omniinfra``. Reproduced here by chdir-ing into a directory stripped of
    write permission for this process. Pre-fix, ``handle()`` tried to
    ``mkdir('.onex_state')`` under it and raised ``PermissionError``. Post-fix
    the handler touches no filesystem at all, so the cwd's mode is irrelevant.
    """
    if os.geteuid() == 0:  # pragma: no cover — CI runners are not root
        pytest.skip("root ignores the write bit; the permission shape cannot be posed")

    readonly = tmp_path / "app"
    readonly.mkdir()
    readonly.chmod(stat.S_IRUSR | stat.S_IXUSR)  # r-x------ : no write bit
    monkeypatch.chdir(readonly)
    try:
        result = HandlerSessionPhaseReducer().handle(_started())
    finally:
        readonly.chmod(stat.S_IRWXU)

    assert result["projections"][0]["session_id"] == _SESSION
    assert list(readonly.iterdir()) == [], (
        "the fold created something on disk — the state of record must be the "
        "database row, not a local file"
    )


@pytest.mark.integration
def test_prior_state_comes_from_the_durable_row(tmp_path: Path) -> None:
    """A second event folds onto the row the first one persisted.

    This is the whole reason the node needs prior-state provisioning: a bus
    message carries ONE event and never the prior state (no wire schema on any
    of the three subscribed topics declares a ``state`` field). Without the row,
    ``session.phase.state`` hits ``delta``'s ``state is None`` branch and
    clobbers a live session's phase with ``"unknown"``.
    """
    store = StateIoRowStore()
    handler = HandlerSessionPhaseReducer()

    with state_io_dispatch(store, _SESSION):
        handler.handle(_started())
    with state_io_dispatch(store, _SESSION):
        handler.handle(_phase_advance())

    persisted = store.load(_SESSION)
    assert persisted is not None, "the fold persisted no durable row"
    assert persisted.current_phase == "merge", (
        "the second fold did not see the first fold's row — it re-initialised "
        "instead of folding"
    )
    assert persisted.phase_index == 3
    assert persisted.budget_elapsed_pct == 55
    assert list(tmp_path.iterdir()) == [], "a fold wrote to the filesystem"


@pytest.mark.integration
def test_row_is_keyed_on_session_id_and_carries_the_denormalized_columns() -> None:
    """The persisted payload exposes exactly the keys the runtime's seam reads.

    ``handler_wiring._extract_state_io_metadata`` pulls three well-known
    top-level keys off an otherwise-opaque payload to populate the durable row's
    indexed columns. ``state`` must be the session FSM's own state
    (``current_phase``); ``in_flight`` must be False, because a reducer fold is
    complete when it returns and has no committed-but-unpublished emission to
    recover.
    """
    import json

    store = StateIoRowStore()
    with state_io_dispatch(store, _SESSION):
        HandlerSessionPhaseReducer().handle(_started())

    raw, version = store.rows[_SESSION]
    payload = json.loads(raw)
    assert payload["session_id"] == _SESSION, "the row is not keyed on session_id"
    assert payload["state"] == "health_gate"
    assert payload["in_flight"] is False
    assert payload["tenant_id"] == ""
    assert version == 1


@pytest.mark.integration
def test_codec_round_trips_the_injected_keys() -> None:
    """``decode(encode(state))`` is the identity.

    ``ModelSessionPhaseState`` declares ``extra="forbid"`` and has no field named
    ``state`` / ``in_flight`` / ``tenant_id``, so ``decode`` must strip exactly
    the keys ``encode`` injects. A drift between the two would surface as a
    validation error on the first reload of a real row.
    """
    state = ModelSessionPhaseState(
        session_id=_SESSION,
        current_phase="merge",
        phase_index=3,
        phase_started_at=_T1,
        budget_elapsed_pct=55,
        active_worker_count=4,
        exit_conditions_met=("pr_merged",),
        exit_conditions_pending=("ci_green",),
        last_evaluation="budget_warning",
        last_tick_at=_T1,
    )
    assert decode(encode(state)) == state


@pytest.mark.integration
def test_outside_a_dispatch_there_is_no_prior_state_and_no_local_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The CLI / unit shape: a preview fold that reads and writes nothing.

    Deliberate, per the ruling — a process-local dict or a local file here would
    reintroduce a state of record outside the database. The fold still returns a
    correct projection; it simply has no prior state to fold onto.
    """
    monkeypatch.chdir(tmp_path)
    result = HandlerSessionPhaseReducer().handle(_phase_advance())

    assert result["projections"][0]["current_phase"] == "unknown", (
        "a non-start event with no prior state must yield the idle sentinel"
    )
    assert list(tmp_path.iterdir()) == [], "the preview fold wrote to disk"


@pytest.mark.integration
def test_handle_has_no_state_path_parameter() -> None:
    """The ``.onex_state`` path is deleted, not shimmed.

    A surviving ``state_path=`` keyword would be invisible to bus dispatch (it
    would always take its default) while still letting a caller reintroduce the
    file. AC3: the handler stops doing I/O.
    """
    import inspect

    from omnimarket.nodes.node_session_phase_reducer.handlers import (
        handler_session_phase_reducer as module,
    )

    signature = inspect.signature(HandlerSessionPhaseReducer().handle)
    assert list(signature.parameters) == ["request"], (
        f"handle() must take exactly the def-B request parameter, got "
        f"{list(signature.parameters)}"
    )
    assert not hasattr(module, "_DEFAULT_STATE_PATH"), (
        "the module still declares the deleted default state-file path"
    )
    for removed in ("_read_state", "_write_state"):
        assert not hasattr(HandlerSessionPhaseReducer, removed), (
            f"HandlerSessionPhaseReducer.{removed} is filesystem I/O and must be gone"
        )


@pytest.mark.integration
def test_unbound_fold_does_not_leak_into_a_later_unbound_fold(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unbound write must never be readable as prior state (CodeRabbit).

    ``get_default_proxy()`` is a process-wide singleton. Before the fix, an
    unbound ``__setitem__`` cached ``(None, state)``, and a LATER unbound fold
    resolved its own source to ``None`` too — so the identity guard
    ``cached[0] is payload_json`` compared ``None is None``, matched, and handed
    back the earlier fold's state as prior state. That is a state of record
    living outside the database, in process memory, across dispatches: exactly
    what the OMN-16924 ruling removes.

    Two unbound folds of a NON-start event. Each must independently see no prior
    state and return the idle sentinel; the second must not inherit the first.
    """
    monkeypatch.chdir(tmp_path)
    reset_default_proxy()
    handler = HandlerSessionPhaseReducer()

    first = handler.handle(_started())
    assert first["projections"][0]["current_phase"] == "health_gate"

    second = handler.handle(_phase_advance())
    assert second["projections"][0]["current_phase"] == "unknown", (
        "the second unbound fold inherited the first fold's state from the "
        "process-wide proxy — an unbound write must be dropped, not cached"
    )
    assert get_default_proxy()._cache == {}, (
        "an unbound write left an entry in the shared cache"
    )


@pytest.mark.integration
def test_abandoned_dispatch_state_is_not_folded_onto_by_the_next_dispatch() -> None:
    """A raising fold must not leave prior state behind for the next dispatch.

    The runtime calls ``codec.flush`` only AFTER ``handle()`` returns, so a
    dispatch that raises mid-fold leaves its ``__setitem__`` entry in the shared
    cache. Before the fix the entry survived whenever no row existed yet for
    that session: both the abandoned entry's source and the next dispatch's
    source were ``None``, the identity guard matched, and the next dispatch
    folded onto a state the database never committed.

    Here dispatch 1 writes and then raises before flush; dispatch 2 for the same
    session, with the row still absent, must see NO prior state.
    """
    store = StateIoRowStore()
    handler = HandlerSessionPhaseReducer()
    started = _started()
    session_id = started.session_id

    def abandon_after_write() -> None:
        """Write inside a bound dispatch, then raise before the runtime flushes."""
        with state_io_dispatch(store, session_id):
            handler.handle(started)
            raise RuntimeError("fold abandoned after the write, before flush")

    reset_default_proxy()
    with pytest.raises(RuntimeError, match="fold abandoned"):
        abandon_after_write()

    assert store.load(session_id) is None, "an abandoned dispatch must persist nothing"

    with state_io_dispatch(store, session_id):
        result = handler.handle(_phase_advance())

    assert result["projections"][0]["current_phase"] == "unknown", (
        "the retry folded onto state from the abandoned dispatch instead of "
        "treating the absent row as no prior state"
    )
