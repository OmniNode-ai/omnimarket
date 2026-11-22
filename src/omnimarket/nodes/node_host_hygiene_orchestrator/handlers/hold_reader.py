# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Which hosts their own capacity guard holds (OMN-24610).

A host hygiene run reads its hold reader before the decision compute, so a held
host's cleanups are decided as held instead of dispatched. The holds are the
same field lab work placement reads: a pool host's capacity advertisement
carries ``capacity_refusal`` (ModelHostCapacityAdvertisement.capacity_refusal),
which the lane host pool snapshot reads as ``capacity: <refusal>``.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime

from omnimarket.models.model_host_capacity_advertisement import (
    ModelHostCapacityAdvertisement,
)
from omnimarket.nodes.node_lane_host_pool_snapshot_effect.protocols.protocol_pool_snapshot import (
    ProtocolHostAdvertisementSource,
)


class InMemoryHostHoldReader:
    """Returns a copy of the holds it was given.

    The test double and the default when nothing is wired: nobody is held.
    """

    def __init__(self, held: Mapping[str, str] | None = None) -> None:
        self._held: dict[str, str] = dict(held) if held is not None else {}

    def held_hosts(self, now: datetime, max_age_seconds: int) -> dict[str, str]:
        return dict(self._held)


class AdvertisedHostHoldReader:
    """Reads the holds from the host capacity advertisements.

    The newest advertisement per host is what that host last said about
    itself. One whose age at ``now`` is past ``max_age_seconds`` is stale: a
    stale advertisement says nothing about a host now, so it holds nobody. The
    remaining advertisements with a non-empty ``capacity_refusal`` hold their
    host under that refusal.
    """

    def __init__(self, source: ProtocolHostAdvertisementSource) -> None:
        self._source = source

    def held_hosts(self, now: datetime, max_age_seconds: int) -> dict[str, str]:
        newest: dict[str, ModelHostCapacityAdvertisement] = {}
        for advertisement in self._source.read_advertisements():
            current = newest.get(advertisement.host_name)
            if current is None or advertisement.advertised_at > current.advertised_at:
                newest[advertisement.host_name] = advertisement
        held: dict[str, str] = {}
        for host_name, advertisement in newest.items():
            if advertisement.capacity_refusal == "":
                continue
            age = (now - advertisement.advertised_at).total_seconds()
            if age > max_age_seconds:
                continue
            held[host_name] = advertisement.capacity_refusal
        return held


__all__: list[str] = ["AdvertisedHostHoldReader", "InMemoryHostHoldReader"]
