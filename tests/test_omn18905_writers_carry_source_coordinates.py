# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-18905: the reader half of a coordinated additive injection.

Every in-process projection writer builds its ``MessageMeta`` from
``_partition``/``_offset`` in the dict the runtime hands ``handle()``, and
stamps the snapshot delta it publishes with those coordinates.
``SnapshotCache`` drops a delta whose ``source_offset`` is not greater than
the cached one from the same source topic and partition, so those two keys
decide whether a second delta for the same key is applied or discarded.

The producer does not inject them yet. That is the point of landing this
first: a key the stripper covers early is inert, while one it covers late is
a silent, offset-committing drop, so consumer-first is the only safe order
(the reasoning is spelled at ``PENDING_UPSTREAM_INJECTED_KEYS``).

These tests pin the READER contract so the producer change lands into
something already proven, and so a later edit cannot quietly drop the keys on
the floor while the injection keeps working.

Measured on the `.201` dev lane 2026-09-20: with neither key injected, a
trace subscribed to `onex.snapshot.projection.runner-fleet.v1` alone replayed
to the end of the topic (position 16,698) and still served rows stamped
`06:06:13Z`, because every delta -- snapshot offsets 0-2 and 16764-16766
alike -- carried `source_offset 0`. First writer wins forever.
"""

from __future__ import annotations

import importlib
from typing import Any

import pytest
import yaml

from omnimarket.projection.handler_shim import (
    INJECTED_OFFSET_KEY,
    INJECTED_PARTITION_KEY,
    PENDING_UPSTREAM_INJECTED_KEYS,
    RUNTIME_INJECTED_KEYS,
)

pytestmark = pytest.mark.unit

#: The four classes the OMN-16874 roster declares for in-process dispatch.
#: Walked from the contracts rather than hand-listed, so a fifth writer is
#: covered the day it lands.
_ATTR = "onex_runtime_inprocess_dispatch"


def _inprocess_writers() -> list[type[Any]]:
    import pathlib

    nodes = pathlib.Path(__file__).parent.parent / "src/omnimarket/nodes"
    found: list[type[Any]] = []
    for node_dir in sorted(nodes.glob("node_projection_*")):
        contract = node_dir / "contract.yaml"
        if not contract.exists():
            continue
        routing = yaml.safe_load(contract.read_text()).get("handler_routing") or {}
        for entry in routing.get("handlers", []):
            ref = entry.get("handler") or {}
            name, module = ref.get("name"), ref.get("module")
            if not name or not module:
                continue
            klass = getattr(importlib.import_module(module), name, None)
            if klass is not None and getattr(klass, _ATTR, False):
                found.append(klass)
    return found


def test_the_walk_finds_the_writers_at_all() -> None:
    """Guard against an inert suite: an empty walk would pass everything."""
    writers = _inprocess_writers()
    assert len(writers) >= 4, [w.__name__ for w in writers]


def test_every_inprocess_writer_reads_both_source_coordinates() -> None:
    """The reader contract, asserted on source rather than by running Kafka.

    Each writer's synchronous entry pops both keys out of the injected dict.
    A writer that stopped doing so would publish fixed coordinates again and
    re-enter the first-writer-wins failure, silently.
    """
    import inspect

    for writer in _inprocess_writers():
        source = inspect.getsource(writer.handle)
        assert INJECTED_PARTITION_KEY in source, (
            f"{writer.__name__}.handle does not read {INJECTED_PARTITION_KEY!r}; "
            "its deltas would carry a fixed partition"
        )
        assert INJECTED_OFFSET_KEY in source, (
            f"{writer.__name__}.handle does not read {INJECTED_OFFSET_KEY!r}; "
            "its deltas would carry a fixed offset and every one after the "
            "first for a given key would be dropped as a replay"
        )


def test_a_writer_stamps_the_injected_coordinates_onto_its_meta() -> None:
    """Behavioural, not textual: the keys reach MessageMeta.

    The source assertion above catches a deletion; this catches the subtler
    case where the keys are read and then not threaded through.
    """
    from omnimarket.nodes.node_projection_lab_lane_health.handlers.handler_lab_lane_health_runner import (
        LabLaneHealthProjectionWriter,
    )

    seen: dict[str, Any] = {}

    class _Recording(LabLaneHealthProjectionWriter):
        @property
        def db(self) -> Any:
            class _Db:
                async def connect(self) -> None: ...

                async def close(self) -> None: ...

            return _Db()

        async def _project_and_report(  # type: ignore[override]
            self, topic: str, data: dict[str, Any], meta: Any
        ) -> list[Any]:
            seen["partition"] = meta.partition
            seen["offset"] = meta.offset
            return []

    _Recording().handle(
        {
            "lane": "compose-dev",
            "timestamp": "2026-09-20T10:05:45+00:00",
            "_topic": "onex.evt.omnibase-infra.runtime-health-check.v1",
            INJECTED_PARTITION_KEY: 3,
            INJECTED_OFFSET_KEY: 4242,
        }
    )

    assert seen == {"partition": 3, "offset": 4242}


def test_absent_coordinates_keep_todays_behaviour() -> None:
    """Explicitly pinned, because the producer does not inject them yet.

    Until the injection lands, every dispatch arrives without these keys and
    must keep working exactly as it does now. This is what makes the reader
    change inert rather than a flag day.
    """
    from omnimarket.nodes.node_projection_lab_lane_health.handlers.handler_lab_lane_health_runner import (
        LabLaneHealthProjectionWriter,
    )

    seen: dict[str, Any] = {}

    class _Recording(LabLaneHealthProjectionWriter):
        @property
        def db(self) -> Any:
            class _Db:
                async def connect(self) -> None: ...

                async def close(self) -> None: ...

            return _Db()

        async def _project_and_report(  # type: ignore[override]
            self, topic: str, data: dict[str, Any], meta: Any
        ) -> list[Any]:
            seen["partition"] = meta.partition
            seen["offset"] = meta.offset
            seen["payload_keys"] = sorted(data)
            return []

    _Recording().handle(
        {
            "lane": "compose-dev",
            "timestamp": "2026-09-20T10:05:45+00:00",
            "_topic": "onex.evt.omnibase-infra.runtime-health-check.v1",
        }
    )

    assert seen["partition"] == 0
    assert seen["offset"] == 0
    # And the transport keys never reach the fold in either direction.
    assert seen["payload_keys"] == ["lane", "timestamp"]


def test_both_keys_are_declared_consumer_first() -> None:
    """The stripper covers them now; the producer catches up later.

    The seam guard requires a pending key to also be in the stripper set, so
    these two assertions together are what make the additive pair landable in
    the safe order.
    """
    assert INJECTED_PARTITION_KEY in RUNTIME_INJECTED_KEYS
    assert INJECTED_OFFSET_KEY in RUNTIME_INJECTED_KEYS
    assert INJECTED_PARTITION_KEY in PENDING_UPSTREAM_INJECTED_KEYS
    assert INJECTED_OFFSET_KEY in PENDING_UPSTREAM_INJECTED_KEYS
    # The pending entries are deleted when the omnibase_infra floor rises to
    # the release carrying the injection; the seam guard turns a stale entry
    # red rather than leaving it as a permanent exemption.
    assert PENDING_UPSTREAM_INJECTED_KEYS <= RUNTIME_INJECTED_KEYS
