# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Alarm channels behind one protocol (OMN-19998, 17a).

A channel sends one alarm and returns a receipt id; a later call reads that receipt back. A send is "delivered" only
when a read-back proves it, never because the send call returned. Three kinds exist, chosen by the overlay:

* ``in_memory``: delivers to a list; the test and default backend, with switches to simulate a channel that is down
  or one that accepts a send and never delivers.
* ``unconfigured``: a channel nobody has authorised or given a sender. Every send fails with a typed reason, so the
  alarm goes to the second channel at once. This is the primary channel until the operator names one.
* ``floor_banner``: the alarm is drawn on the floor page. Its read-back fetches the served floor record and looks for
  the alarm id there, so "delivered" means the page a person reads shows the alarm.

The protocol belongs in omnibase_spi once a release of it is cut; until then it is declared here, beside its only users.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import datetime
from typing import Literal, Protocol

from omnimarket.models.floor_17a import ModelAlarm, ModelAlarmChannelDef

Delivery = Literal["pending", "delivered", "failed"]


class AlarmSendRefusedError(RuntimeError):
    """A channel could not take the alarm; the message is the typed reason."""


class ProtocolAlarmChannel(Protocol):
    def send(self, alarm: ModelAlarm, now: datetime) -> str:
        """Hand the alarm to the channel and return its receipt id; raises ``AlarmSendRefusedError``."""
        ...

    def read_receipt(self, receipt_id: str, now: datetime) -> Delivery:
        """The delivery state of a send, read back from the channel."""
        ...


class InMemoryAlarmChannel:
    """Delivers to a list. ``down`` refuses every send; ``black_hole`` accepts a send and never delivers it."""

    def __init__(self, *, down: bool = False, black_hole: bool = False) -> None:
        self.down = down
        self.black_hole = black_hole
        self.sent: list[ModelAlarm] = []

    def send(self, alarm: ModelAlarm, now: datetime) -> str:
        if self.down:
            raise AlarmSendRefusedError("the channel is down")
        self.sent.append(alarm)
        return f"mem:{alarm.alarm_id}"

    def read_receipt(self, receipt_id: str, now: datetime) -> Delivery:
        if self.black_hole:
            return "pending"
        delivered = {f"mem:{a.alarm_id}" for a in self.sent}
        return "delivered" if receipt_id in delivered else "failed"


class UnconfiguredAlarmChannel:
    """The channel nobody has authorised: every send is refused with the reason, so the second channel takes over."""

    def __init__(self, channel_id: str) -> None:
        self.channel_id = channel_id

    def send(self, alarm: ModelAlarm, now: datetime) -> str:
        raise AlarmSendRefusedError(
            f"channel {self.channel_id} has no authorised sender: it needs a channel and a sender from the operator"
        )

    def read_receipt(self, receipt_id: str, now: datetime) -> Delivery:
        return "failed"


class FloorBannerAlarmChannel:
    """The floor page itself. ``served_alarm_ids`` reads which alarm ids the served floor record carries."""

    def __init__(self, served_alarm_ids: Callable[[], set[str] | None]) -> None:
        self._served = served_alarm_ids

    def send(self, alarm: ModelAlarm, now: datetime) -> str:
        # The alarm is in the open set the next floor record draws; the receipt is its own id.
        return alarm.alarm_id

    def read_receipt(self, receipt_id: str, now: datetime) -> Delivery:
        served = self._served()
        if served is None:
            return "pending"
        return "delivered" if receipt_id in served else "pending"


def build_channels(
    definitions: tuple[ModelAlarmChannelDef, ...],
    *,
    served_alarm_ids: Callable[[], set[str] | None],
    in_memory: Mapping[str, InMemoryAlarmChannel] | None = None,
) -> dict[str, ProtocolAlarmChannel]:
    """One channel object per overlay definition. An ``in_memory`` channel is taken from ``in_memory`` when given."""
    built: dict[str, ProtocolAlarmChannel] = {}
    for definition in definitions:
        if definition.kind == "in_memory":
            built[definition.id] = (in_memory or {}).get(
                definition.id, InMemoryAlarmChannel()
            )
        elif definition.kind == "floor_banner":
            built[definition.id] = FloorBannerAlarmChannel(served_alarm_ids)
        else:
            built[definition.id] = UnconfiguredAlarmChannel(definition.id)
    return built
