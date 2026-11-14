# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Count a bus's consumer groups by state and fail over the contract's Empty-group threshold.

Every Empty group is replayed from ``__consumer_offsets`` when the broker starts and
scanned by its transaction-abort loop. The dev redpanda segfaulted three times on
2026-10-31 holding 19,154 of them, and the CI bus crash-looped 681 times holding
about 15,400 (OMN-21764, OMN-21818). The threshold is declared in this node's
``contract.yaml`` (``consumer_group_guard.empty_group_max``) and read from there at
every call; no module constant carries it.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from importlib import resources

import yaml

from omnimarket.models.consumer_group_guard import (
    ModelConsumerGroupCensus,
    ModelConsumerGroupGuardRequest,
)
from omnimarket.nodes.node_runtime_coverage_effect.handlers.handler_runtime_coverage import (
    resolve_broker,
)

_TIMEOUT = 20.0
_LAG_PROBE_GROUP = "onex.consumer-group-guard.lag-probe"
_NODE = "node_consumer_group_guard_effect"


def contract_threshold() -> int:
    """``consumer_group_guard.empty_group_max`` from this node's contract."""
    contract = yaml.safe_load(
        resources.files(f"omnimarket.nodes.{_NODE}")
        .joinpath("contract.yaml")
        .read_text()
    )
    value = contract["consumer_group_guard"]["empty_group_max"]
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError("consumer_group_guard.empty_group_max must be a count")
    return value


def list_group_states(client_config: Mapping[str, str]) -> dict[str, str]:
    """Admin-only listing of group name to UPPER-CASE state; never joins, commits or writes."""
    from confluent_kafka.admin import AdminClient

    admin = AdminClient(dict(client_config))
    listing = admin.list_consumer_groups(request_timeout=_TIMEOUT).result(
        timeout=_TIMEOUT
    )
    if listing.errors:
        # A partial listing under-counts, so it is not a census.
        raise RuntimeError("broker group listing incomplete")
    return {
        g.group_id: str(getattr(g.state, "name", g.state)).rsplit(".", 1)[-1].upper()
        for g in listing.valid
    }


def lag_group_patterns() -> tuple[str, ...]:
    """``consumer_group_guard.lag.group_patterns`` from this node's contract: the groups whose lag a census reads."""
    contract = yaml.safe_load(
        resources.files(f"omnimarket.nodes.{_NODE}")
        .joinpath("contract.yaml")
        .read_text()
    )
    value = contract["consumer_group_guard"]["lag"]["group_patterns"]
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise ValueError(
            "consumer_group_guard.lag.group_patterns must be a list of patterns"
        )
    for pattern in value:
        re.compile(pattern)
    return tuple(value)


def sum_lag(
    committed: Mapping[tuple[str, int], int],
    watermarks: Mapping[tuple[str, int], tuple[int, int]],
) -> int:
    """Messages behind over the partitions a group has committed on: end offset minus committed offset.

    A negative committed offset means the group never committed on that partition; every message still
    held there (end minus start) counts as behind. A partition with no watermark reading is an error.
    """
    total = 0
    for key, offset in committed.items():
        if key not in watermarks:
            raise RuntimeError("no watermark reading for a committed partition")
        low, high = watermarks[key]
        total += max(0, high - (offset if offset >= 0 else low))
    return total


def read_group_lag(
    client_config: Mapping[str, str], group_names: list[str]
) -> dict[str, int]:
    """Admin-only lag per group: committed offsets from the broker against each partition's end offset.

    The probe consumer never subscribes, so it does not join a group or commit; a group whose offsets
    could not be read has no entry.
    """
    from confluent_kafka import Consumer, ConsumerGroupTopicPartitions, TopicPartition
    from confluent_kafka.admin import AdminClient

    if not group_names:
        return {}
    admin = AdminClient(dict(client_config))
    futures = admin.list_consumer_group_offsets(
        [ConsumerGroupTopicPartitions(name) for name in group_names],
        request_timeout=_TIMEOUT,
    )
    probe = Consumer(
        {
            **dict(client_config),
            "group.id": _LAG_PROBE_GROUP,
            "enable.auto.commit": False,
        }
    )
    lag: dict[str, int] = {}
    try:
        for name, future in futures.items():
            try:
                partitions = future.result(timeout=_TIMEOUT).topic_partitions
                committed = {(tp.topic, tp.partition): tp.offset for tp in partitions}
                watermarks = {
                    key: probe.get_watermark_offsets(
                        TopicPartition(key[0], key[1]), timeout=_TIMEOUT
                    )
                    for key in committed
                }
                lag[name] = sum_lag(committed, watermarks)
            except Exception:
                continue
    finally:
        probe.close()
    return lag


def read_group_states(client_config: Mapping[str, str]) -> dict[str, int]:
    """Admin-only listing: counts by upper-case state; never joins, commits or writes."""
    states = list_group_states(client_config)
    counts: dict[str, int] = {}
    for state in states.values():
        counts[state] = counts.get(state, 0) + 1
    return counts


class HandlerConsumerGroupGuardEffect:
    def __init__(
        self,
        state_reader: Callable[[Mapping[str, str]], dict[str, int]] | None = None,
        broker_resolver: Callable[[str], tuple[str, str, dict[str, str]]] | None = None,
        threshold_reader: Callable[[], int] | None = None,
    ) -> None:
        self._read = state_reader or read_group_states
        self._resolve = broker_resolver or resolve_broker
        self._threshold = threshold_reader or contract_threshold

    async def handle(
        self, request: ModelConsumerGroupGuardRequest
    ) -> ModelConsumerGroupCensus:
        limit = self._threshold()
        broker = ""
        try:
            broker, _environment, config = self._resolve(request.bus_lane)
            counts = await asyncio.to_thread(self._read, config)
        except Exception as exc:
            # Error text from a client may carry connection credentials: keep the class only.
            return ModelConsumerGroupCensus(
                bus_lane=request.bus_lane,
                broker=broker,
                observed_at=datetime.now(UTC),
                status="FAIL",
                empty_group_max=limit,
                detail="group census unreadable, so the count is unknown",
                error=f"group census unreadable: {type(exc).__name__}",
            )
        total = sum(counts.values())
        empty = counts.get("EMPTY", 0)
        over = empty > limit
        return ModelConsumerGroupCensus(
            bus_lane=request.bus_lane,
            broker=broker,
            observed_at=datetime.now(UTC),
            status="FAIL" if over else "OK",
            groups_total=total,
            groups_empty=empty,
            groups_other=total - empty,
            empty_group_max=limit,
            detail=(
                f"{empty} Empty consumer groups of {total}, "
                f"{'over' if over else 'within'} the contract's {limit}"
            ),
        )
