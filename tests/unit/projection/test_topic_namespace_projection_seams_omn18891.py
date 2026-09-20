# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The projection seams honour the deployment topic namespace (OMN-18891).

These two are the seams a slot cannot survive without. The projection runner
subscribes to its contract topics directly and the snapshot cache subscribes
to its exposure keys directly, so both bypass the canonical topic resolver
entirely. A slot built without them would run its projection writers and its
projection API against the DEV LANE's topics, which is the precise failure
the isolation exists to prevent.

The snapshot cache carries a second, quieter defect of its own. Its state map
is keyed by the CANONICAL exposure topic, and every lookup on the consume
path passes the message's PHYSICAL topic. The ticket says a prefixed name
raises there; it does not. ``apply_message`` returns at its
``state is None`` guard and the cache silently serves stale rows with nothing
in the log, which is worse than raising because nothing is observable. Both
directions are asserted below.
"""

from __future__ import annotations

import json

import pytest
from omnibase_infra.topics.topic_namespace import TOPIC_NAMESPACE_ENV_VAR

from omnimarket.projection.models import ProjectionTableConfig
from omnimarket.projection.snapshot_cache import SnapshotCache

pytestmark = [pytest.mark.unit]

TOPIC_A = "onex.snapshot.projection.live-events.v1"
TOPIC_B = "onex.snapshot.projection.registration.v1"
SLOT = "prepr1"


def _exposure(topic: str) -> ProjectionTableConfig:
    return ProjectionTableConfig(
        topic=topic,
        table="test_table",
        columns=("id", "value"),
        bus_backed=True,
        key_columns=("id",),
        limit=100,
    )


def _delta_payload() -> bytes:
    """One valid upsert delta, as the runner publishes it."""
    return json.dumps(
        {
            "topic": TOPIC_A,
            "key": ["row-1"],
            "op": "upsert",
            "row": {"id": "row-1", "value": "v"},
            "observed_at": "2026-09-20T00:00:00Z",
            "source_event_id": "00000000-0000-0000-0000-000000000001",
            "source_topic": "onex.evt.platform.node-registration.v1",
            "source_partition": 0,
            "source_offset": 1,
        }
    ).encode("utf-8")


def _cache(*topics: str) -> SnapshotCache:
    # An explicit group id, because the default derivation reads
    # ONEX_ENVIRONMENT fail-fast and this module is about topic names rather
    # than group names. Passing one keeps the test independent of an
    # environment variable it does not exercise -- the first revision omitted
    # it, passed locally where the variable happens to be set, and failed in
    # CI with KeyError: 'ONEX_ENVIRONMENT'.
    return SnapshotCache(
        exposures={t: _exposure(t) for t in topics},
        bootstrap_servers="localhost:9092",
        group_id="omn18891-namespace-probe",
    )


# ---------------------------------------------------------------------------
# Unset: byte-identical pass-through
# ---------------------------------------------------------------------------


def test_cache_subscribes_to_canonical_topics_when_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(TOPIC_NAMESPACE_ENV_VAR, raising=False)
    cache = _cache(TOPIC_A, TOPIC_B)
    assert cache.subscription_topics == [TOPIC_A, TOPIC_B]


def test_cache_canonical_mapping_is_identity_when_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(TOPIC_NAMESPACE_ENV_VAR, raising=False)
    cache = _cache(TOPIC_A)
    assert cache.canonical_topic(TOPIC_A) == TOPIC_A


# ---------------------------------------------------------------------------
# Set: the wire moves, the index does not
# ---------------------------------------------------------------------------


def test_cache_subscribes_to_physical_topics_when_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without this the projection API reads the shared lane's snapshots."""
    monkeypatch.setenv(TOPIC_NAMESPACE_ENV_VAR, SLOT)
    cache = _cache(TOPIC_A, TOPIC_B)
    assert cache.subscription_topics == [f"{SLOT}.{TOPIC_A}", f"{SLOT}.{TOPIC_B}"]


def test_cache_state_stays_keyed_by_the_canonical_topic(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The exposure map is the contract; the namespace is a wire fact."""
    monkeypatch.setenv(TOPIC_NAMESPACE_ENV_VAR, SLOT)
    cache = _cache(TOPIC_A)
    assert set(cache.bus_backed_topics) == {TOPIC_A}


def test_cache_maps_a_physical_topic_back_before_the_lookup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(TOPIC_NAMESPACE_ENV_VAR, SLOT)
    cache = _cache(TOPIC_A)
    assert cache.canonical_topic(f"{SLOT}.{TOPIC_A}") == TOPIC_A
    # Tolerant of an already-canonical name, so the consume path is correct
    # whichever form it is handed during a roll-out.
    assert cache.canonical_topic(TOPIC_A) == TOPIC_A


def test_apply_message_reaches_its_exposure_under_a_physical_topic(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The defect this closes is SILENT, which is why it is asserted here.

    Pre-fix, ``apply_message`` handed a physical topic name to a state map
    keyed canonically, missed, and returned at the ``state is None`` guard.
    The cache then served stale rows with nothing in the log and a healthy
    consumer lag. The positive control below is the canonical call, so a
    blanket "apply_message never applies anything" regression cannot pass
    this test.
    """
    monkeypatch.setenv(TOPIC_NAMESPACE_ENV_VAR, SLOT)
    payload = _delta_payload()

    # Positive control: the canonical name applies. Without it, a regression
    # that made apply_message a no-op would read as this test passing.
    canonical_cache = _cache(TOPIC_A)
    canonical_cache.apply_message(TOPIC_A, b"row-1", payload, [])
    assert canonical_cache.row_count(TOPIC_A) == 1, (
        "positive control: a canonical apply must land"
    )

    physical_cache = _cache(TOPIC_A)
    physical_cache.apply_message(f"{SLOT}.{TOPIC_A}", b"row-1", payload, [])
    assert physical_cache.row_count(TOPIC_A) == 1, (
        "a physical topic must reach the exposure registered under the "
        "canonical name, not be dropped at the state lookup"
    )
