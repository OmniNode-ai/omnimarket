# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Drain stores: in memory (the default) and on the bus (the real profile).

A drain mark is time-bounded: a draining record whose ``until`` is at or behind the store's clock reads as not
draining, in ``is_draining``, in ``draining_hosts`` and in ``until_of``.

The bus store keeps no state of its own and writes no file. A write is a drain command on the bus, taken by the addressed
host's serve process, which advertises the mark on every capacity advertisement; a read is the host's newest advertisement.
Every launcher and the lab fill read the same advertisement, so the mark is the fleet's and not one machine's. A host that
advertised nothing in the window has an unread mark: the read raises OSError, never an empty answer.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime

from omnimarket.models.model_host_drain_command import ModelHostDrainCommand
from omnimarket.nodes.node_host_drain_effect.adapters.bus_drain import (
    host_advertisements,
)
from omnimarket.nodes.node_host_drain_effect.models.model_host_drain import (
    ModelDrainMark,
)
from omnimarket.nodes.node_host_drain_effect.protocols import (
    ProtocolAdvertisementSource,
    ProtocolDrainCommandPublisher,
)
from omnimarket.nodes.node_lab_work_unit_effect.protocols.local_host_reader import (
    draining_until_from_tools,
)

#: A record per host: whether it reads draining and when its mark expires (None: no expiry).
Record = tuple[bool, datetime | None]


def _now() -> datetime:
    return datetime.now(UTC)


def _as_utc(value: datetime) -> datetime:
    """A naive stamp reads as UTC, so it never compares against an aware clock."""
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _reads_draining(record: Record | None, now: datetime) -> bool:
    """Whether the record reads as draining now: draining, and not past its until."""
    if record is None:
        return False
    draining, until = record
    return draining and (until is None or now < _as_utc(until))


class InMemoryDrainStore:
    #: A mark here dies with the process: it can never back a SAFE reading.
    durable = False

    def __init__(self, *, clock: Callable[[], datetime] = _now) -> None:
        self._clock = clock
        self._records: dict[str, Record] = {}

    def set_draining(
        self,
        host_name: str,
        draining: bool,
        *,
        reason: str,
        at: datetime,
        until: datetime | None = None,
    ) -> None:
        self._records[host_name] = (draining, until)

    def is_draining(self, host_name: str) -> bool:
        return _reads_draining(self._records.get(host_name), self._clock())

    def draining_hosts(self) -> tuple[str, ...]:
        now = self._clock()
        return tuple(
            sorted(
                h for h, record in self._records.items() if _reads_draining(record, now)
            )
        )

    def until_of(self, host_name: str) -> datetime | None:
        """The mark's until while the host reads draining, else None."""
        record = self._records.get(host_name)
        if record is None or not _reads_draining(record, self._clock()):
            return None
        return None if record[1] is None else _as_utc(record[1])

    def mark_of(self, host_name: str) -> ModelDrainMark | None:
        """The host's record as held, whether or not it is past its until."""
        record = self._records.get(host_name)
        return (
            None
            if record is None
            else ModelDrainMark(draining=record[0], until=record[1])
        )


class BusDrainStore:
    """The drain state on the bus: a write is a drain command, a read is the host's newest capacity advertisement."""

    #: The mark is held by the host's serve process and carried on its advertisements: it outlives the verb's process.
    durable = True

    def __init__(
        self,
        publisher: ProtocolDrainCommandPublisher,
        advertisements: ProtocolAdvertisementSource,
        *,
        clock: Callable[[], datetime] = _now,
    ) -> None:
        self._publisher = publisher
        self._advertisements = advertisements
        self._clock = clock

    def set_draining(
        self,
        host_name: str,
        draining: bool,
        *,
        reason: str,
        at: datetime,
        until: datetime | None = None,
    ) -> None:
        self._publisher.publish(
            ModelHostDrainCommand(
                host_name=host_name,
                draining=draining,
                until=until if draining else None,
                reason=reason,
                issued_at=_as_utc(at),
            )
        )
        self._advertisements.invalidate()

    def mark_of(self, host_name: str) -> ModelDrainMark | None:
        """The mark the host's newest advertisement carries (not draining when it carries none)."""
        ads = host_advertisements(self._advertisements, host_name)
        if not ads:
            raise OSError(
                f"{host_name} advertised nothing in the window: its drain mark is unread"
            )
        until = draining_until_from_tools(ads[-1].tools)
        return ModelDrainMark(draining=until is not None, until=until)

    def is_draining(self, host_name: str) -> bool:
        mark = self.mark_of(host_name)
        return mark is not None and mark.reads_draining(self._clock())

    def until_of(self, host_name: str) -> datetime | None:
        mark = self.mark_of(host_name)
        if mark is None or not mark.reads_draining(self._clock()):
            return None
        return mark.until

    def draining_hosts(self) -> tuple[str, ...]:
        now = self._clock()
        newest: dict[str, tuple[str, ...]] = {}
        for ad in self._advertisements.advertisements():
            newest[ad.host_name] = tuple(ad.tools)
        hosts = []
        for host, tools in newest.items():
            until = draining_until_from_tools(tools)
            if until is not None and now < until:
                hosts.append(host)
        return tuple(sorted(hosts))
