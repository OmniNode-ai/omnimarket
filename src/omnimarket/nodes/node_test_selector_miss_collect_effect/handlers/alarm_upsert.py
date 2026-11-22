# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The alarm's keyed upsert and its retried delivery, shared by the collector and the slot check.

One alarm per key ``(repo, nightly slot date, test file or "*")``. The upsert is atomic by key, so a rerun appends no
second alarm and two overlapping invocations append one. Delivery is retried from the store until the alarm is
``delivered``, and the receiver drops a repeat of the key, so a crash after the upsert and before delivery, or after the
receiver accepted and before the store recorded ``delivered``, leaves one receiver-visible alarm.
"""

from __future__ import annotations

from collections.abc import Iterable

from omnimarket.nodes.node_test_selector_miss_collect_effect.models.model_miss_collect import (
    ModelNightlyAlarm,
)
from omnimarket.nodes.node_test_selector_miss_collect_effect.ports import (
    ProtocolAlarmReceiver,
    ProtocolAlarmStore,
)


def raise_alarms(
    store: ProtocolAlarmStore,
    receiver: ProtocolAlarmReceiver,
    alarms: Iterable[ModelNightlyAlarm],
) -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
    """Upsert each alarm, then deliver everything undelivered. Returns (created, delivered, failed) keys."""
    created = tuple(a.key for a in alarms if store.upsert_pending(a))
    delivered: list[str] = []
    failed: list[str] = []
    for alarm in store.undelivered():
        try:
            receiver.accept(alarm)
        except OSError:
            failed.append(alarm.key)
            continue
        store.mark_delivered(alarm.key)
        delivered.append(alarm.key)
    return created, tuple(delivered), tuple(failed)


__all__: list[str] = ["raise_alarms"]
