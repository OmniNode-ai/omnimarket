# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The measured cause: the live profile read no pool, so a tick on a floor with free slots sent nothing.

Read on the fill host on 2026-11-16: the live profile bound the pool seam to ``UnboundFloorPool``, so every live tick
answered ``UNKNOWN pool-unread:unbound:pool`` and started no lane, while a pool snapshot that the lane host pool
snapshot unit writes every minute sat beside it. The live profile now reads that snapshot (the ``floor_fill_live``
``pool_snapshot_*`` keys): the same tick on the same recorded floor, with free slots in the snapshot, starts a lane.

Golden: a fresh snapshot with two free slots and a floor with ready tasks starts two lanes, each held RESERVED under its
``floor:<line_id>:<task_id>`` key until its CLAIM is read.
Error: a snapshot whose record is older than the bound starts nothing and names ``pool-unread:pool-snapshot``.
Failing control: a snapshot that reads zero free slots starts nothing and is not read as unbound.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from omnimarket.nodes.node_floor_fill_orchestrator.models import (
    EXIT_CODE_BY_STATUS,
    EnumFloorFillStatus,
)
from omnimarket.nodes.node_floor_fill_orchestrator.models.model_floor_fill_config import (
    FloorFillConfigError,
)
from omnimarket.nodes.node_floor_fill_orchestrator.models.model_floor_fill_live_settings import (
    load_floor_fill_live_settings,
)
from omnimarket.nodes.node_floor_fill_orchestrator.orchestration.store import (
    InMemoryFloorStateStore,
)
from omnimarket.nodes.node_floor_fill_orchestrator.tests.support_live_tick import (
    RECORD_MAX_AGE_SECONDS,
    SCOPE,
    SETTINGS_MAX_AGE_SECONDS,
    live_section,
    live_tick,
)

pytestmark = pytest.mark.unit

_tick = live_tick
_live_section = live_section


def _reserved(store: InMemoryFloorStateStore) -> int:
    payload = store.load(SCOPE).payload
    return 0 if payload is None else payload.count('"state":"RESERVED"')


OK = EXIT_CODE_BY_STATUS[EnumFloorFillStatus.OK]
DEGRADED = EXIT_CODE_BY_STATUS[EnumFloorFillStatus.DEGRADED]


def test_measured_cause_sends_a_lane_from_the_pool_snapshot(tmp_path: Path) -> None:
    out, code, store = _tick(tmp_path, free=2, age_seconds=5)
    assert "unbound:pool" not in out
    assert "pool-unread" not in out
    assert code == OK, out
    # A started lane is held RESERVED under its floor key until its CLAIM row is read; two free slots, two starts,
    # whatever the host's cap (5) says.
    assert _reserved(store) == 2, store.load(SCOPE).payload
    assert '"key":"floor:' in (store.load(SCOPE).payload or "")


def test_measured_cause_sends_nothing_from_a_stale_snapshot(tmp_path: Path) -> None:
    out, code, store = _tick(tmp_path, free=2, age_seconds=600)
    assert "pool-unread:pool-snapshot" in out
    assert code == DEGRADED
    assert store.load(SCOPE).version == 0


def test_measured_cause_sends_nothing_when_the_snapshot_has_no_free_slot(
    tmp_path: Path,
) -> None:
    out, code, store = _tick(tmp_path, free=0, age_seconds=5)
    assert "unbound:pool" not in out
    assert "pool-unread" not in out
    assert code == OK, out
    assert _reserved(store) == 0


def test_measured_cause_sends_zero_readable_slots_beside_unread_hosts(
    tmp_path: Path,
) -> None:
    """The measured h051 reading: the token pool status exits 3 on every host, so the snapshot holds 0 slots and an
    ``accounts-unread`` refusal per host. That is an unread pool, not a free count of zero."""
    out, code, store = _tick(tmp_path, free=0, age_seconds=5, unread_hosts=("h3", "h4"))
    assert "pool-unread" not in out, out
    assert code == OK, out
    assert _reserved(store) == 0


def test_measured_cause_sends_counts_a_readable_host_beside_an_unread_one(
    tmp_path: Path,
) -> None:
    out, code, store = _tick(tmp_path, free=2, age_seconds=5, unread_hosts=("h3",))
    assert "pool-unread" not in out, out
    assert code == OK, out
    assert _reserved(store) == 2


def test_measured_cause_sends_does_not_fill_an_away_host_with_slots(
    tmp_path: Path,
) -> None:
    out, code, store = _tick(tmp_path, free=1, age_seconds=5, away_slots=6)
    assert code == OK, out
    assert _reserved(store) == 1, store.load(SCOPE).payload


def test_measured_cause_sends_counts_lane_slots_not_the_cap(tmp_path: Path) -> None:
    out, code, store = _tick(tmp_path, free=1, age_seconds=5, cap=9)
    assert code == OK, out
    assert _reserved(store) == 1


def test_measured_cause_sends_a_non_claude_engine_fills_its_own_hosts(
    tmp_path: Path,
) -> None:
    out, code, store = _tick(
        tmp_path, free=2, age_seconds=5, engine="codex", engines=("claude", "codex")
    )
    assert code == OK, out
    assert _reserved(store) == 2


def test_measured_cause_sends_a_host_without_the_pool_engine_does_not_fill(
    tmp_path: Path,
) -> None:
    """The snapshot's host offers claude only: a codex fill finds no slot and starts nothing."""
    out, code, store = _tick(
        tmp_path, free=2, age_seconds=5, engine="codex", engines=("claude",)
    )
    assert code == OK, out
    assert _reserved(store) == 0


def test_measured_cause_sends_unbound_live_seams_still_send_nothing(
    tmp_path: Path,
) -> None:
    """The live profile still leaves the tickets, brake and stop seams unbound: the recorded floor with two free slots
    answers an unbound seam by name and starts nothing (AC3 is the pool seam only)."""
    out, code, store = _tick(tmp_path, free=2, age_seconds=5, bind_seams=False)
    assert "unbound:" in out, out
    assert "pool-unread" not in out
    assert code == DEGRADED, out
    assert _reserved(store) == 0


def test_measured_cause_sends_an_overlay_without_the_pool_keys_names_them(
    tmp_path: Path,
) -> None:
    """A live overlay written before the pool keys existed refuses naming each of the four, it never half-loads."""
    section = _live_section(tmp_path)
    for name in (
        "pool_snapshot_path",
        "pool_record_suffix",
        "pool_snapshot_max_age_seconds",
        "pool_engine",
    ):
        del section[name]
    with pytest.raises(FloorFillConfigError) as refused:
        load_floor_fill_live_settings(section)
    assert set(refused.value.keys) == {
        f"floor_fill_live.{name}"
        for name in (
            "pool_snapshot_path",
            "pool_record_suffix",
            "pool_snapshot_max_age_seconds",
            "pool_engine",
        )
    }


def test_measured_cause_sends_the_settings_bound_governs_not_the_record_bound(
    tmp_path: Path,
) -> None:
    """The record declares 60 seconds for itself and the settings bound is 180: a snapshot read 120 seconds before the
    injected pool clock is older than the record's own bound and inside the settings', and the tick reads it fresh."""
    assert RECORD_MAX_AGE_SECONDS < 120 < SETTINGS_MAX_AGE_SECONDS
    out, code, store = _tick(tmp_path, free=2, age_seconds=120)
    assert "pool-unread" not in out, out
    assert code == OK, out
    assert _reserved(store) == 2


def test_measured_cause_sends_the_settings_bound_is_inclusive_at_its_edge(
    tmp_path: Path,
) -> None:
    out, code, store = _tick(tmp_path, free=2, age_seconds=SETTINGS_MAX_AGE_SECONDS)
    assert code == OK, out
    assert _reserved(store) == 2


def test_measured_cause_sends_one_second_past_the_settings_bound_is_stale(
    tmp_path: Path,
) -> None:
    out, code, store = _tick(tmp_path, free=2, age_seconds=SETTINGS_MAX_AGE_SECONDS + 1)
    assert "pool-unread:pool-snapshot" in out, out
    assert code == DEGRADED
    assert store.load(SCOPE).version == 0


def test_measured_cause_sends_refuses_a_host_with_empty_engines_and_no_refusal(
    tmp_path: Path,
) -> None:
    """The snapshot writer empties ``engines`` only for a host that has no usable account, and that host carries a
    refusal. A host with empty engines and no refusal is a writer fault (an overlay with no engines): it is an unread
    pool, never a host that counts as zero free slots."""
    out, code, store = _tick(tmp_path, free=2, age_seconds=5, engines=())
    assert "pool-unread:pool-snapshot:" in out, out
    assert "engines" in out, out
    assert code == DEGRADED, out
    assert _reserved(store) == 0


def test_measured_cause_sends_unread_refusal_prefix_is_spelled_once() -> None:
    """The writer, the reader and these tests take the prefix from the one constant: only its own module spells it."""
    from pathlib import Path as _Path

    from omnimarket.nodes.node_lane_host_pool_snapshot_effect.handlers import (
        adapter_snapshot_file,
        handler_pool_snapshot,
        pool_snapshot_refusals,
    )
    from omnimarket.nodes.node_lane_host_pool_snapshot_effect.handlers.pool_snapshot_refusals import (
        ACCOUNTS_UNREAD_PREFIX,
    )

    for module in (adapter_snapshot_file, handler_pool_snapshot):
        text = _Path(str(module.__file__)).read_text(encoding="utf-8")
        assert ACCOUNTS_UNREAD_PREFIX not in text.replace(
            "ACCOUNTS_UNREAD_PREFIX", ""
        ).replace("``accounts-unread``", ""), module.__name__
    assert ACCOUNTS_UNREAD_PREFIX in _Path(
        str(pool_snapshot_refusals.__file__)
    ).read_text(encoding="utf-8")
