# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Live Kafka/Redpanda consumer-group census probe (OMN-14528).

This module is the I/O boundary that REPLACES the hand-typed
``--live-consumer-profiles`` operator CLI flag (OMN-12957). Before this fix the
consumer-liveness census was DATA the operator had to type on the command line;
no automated caller (the ``onex skill runtime_sweep`` dispatch path, CI, or any
scheduled run) ever supplied it, so ``live_consumer_profiles`` was always
``None`` and the deadness check silently skipped with zero findings — the exact
"green over nothing" disease this module closes (see
``reference_detection_shelf_structurally_blind``).

The census is now COLLECTED IN CODE by querying the broker directly for its
LIVE consumer GROUP IDs via ``confluent_kafka.admin.AdminClient`` — the same
client library and probe shape already proven in
``omnibase_infra.services.service_runtime_health_monitor`` for in-container
consumer-group coverage. The caller only supplies a bootstrap-servers
connection string (operational plumbing, e.g. from ``KAFKA_BOOTSTRAP_SERVERS``)
— never the census data itself.

LIVENESS, not mere existence, is the oracle. An ``Empty`` consumer group has
committed offsets but ZERO attached members: the consumer process is DEAD even
though the group id still exists on the coordinator. Counting an ``Empty``
group as "live" would let a dead corpse (a contract that ships in the image,
declares ``subscribe_topics``, and lost its consumer) pass the check — the
precise exists-but-wrong false-negative OMN-14528 must fail RED against. This
probe therefore returns ONLY groups whose state proves attached members
(``STABLE`` / rebalancing), excluding ``EMPTY`` / ``DEAD`` / ``UNKNOWN``.
"""

from __future__ import annotations

import logging

from omnibase_infra.enums.enum_infra_transport_type import EnumInfraTransportType
from omnibase_infra.errors import InfraConnectionError, ModelInfraErrorContext

__all__ = ["collect_live_consumer_groups"]

_log = logging.getLogger(__name__)

# Wall-clock budget for the broker round-trip. A CI/skill invocation must fail
# fast rather than hang indefinitely against an unreachable broker.
_ADMIN_REQUEST_TIMEOUT_S = 10.0
_ADMIN_RESULT_TIMEOUT_S = 15.0

# Group states that prove NO attached consumer members — a group in one of
# these states is a dead/idle corpse, not a live consumer. Mirrors
# ``omnibase_infra.services.service_runtime_health_monitor``'s empty-state set.
_NON_LIVE_STATES = frozenset({"EMPTY", "DEAD", "UNKNOWN"})


def _state_name(state: object) -> str:
    """Normalize a confluent-kafka ConsumerGroupState to a plain uppercase name.

    The confluent client may surface the state as an enum (``.name``) or as a
    ``str``; both collapse to the bare trailing token, uppercased, so the
    liveness filter is robust across client versions.
    """
    enum_name = getattr(state, "name", None)
    raw = str(enum_name if enum_name is not None else state)
    return raw.rsplit(".", maxsplit=1)[-1].upper()


def collect_live_consumer_groups(bootstrap_servers: str) -> list[str]:
    """Return the LIVE (non-Empty) consumer GROUP IDs registered on the broker.

    Uses ``AdminClient.list_consumer_groups`` (KIP-518), which reports every
    group known to the broker's group coordinator together with its state.
    Groups in a non-live state (``EMPTY`` / ``DEAD`` / ``UNKNOWN`` — committed
    offsets but zero attached members) are EXCLUDED: liveness, not mere group
    existence, is the deadness oracle (OMN-14528).

    Args:
        bootstrap_servers: Kafka/Redpanda bootstrap servers connection string
            (e.g. ``<onex-host>:18085``). This is operational plumbing — the
            broker *address* — never the census data itself.

    Returns:
        Sorted list of LIVE consumer group IDs. An empty list is a legal,
        meaningful result (broker reachable, zero LIVE groups exist) and is
        DISTINCT from "census not collected" (``None`` at the request level,
        which the handler treats as a hard failure when the consumer-liveness
        check is required — never a silent skip).

    Raises:
        InfraConnectionError: when ``list_consumer_groups`` reports broker-level
            errors, i.e. the returned listing is incomplete. An incomplete
            census is NOT a census: treating a partial/errored listing as
            complete would silently under-report live groups and false-flag
            healthy consumers, so the collector fails closed.
        Exception: any underlying ``confluent_kafka`` connection/timeout error
            propagates uncaught. A collector that swallowed a broker failure
            into an empty list would reproduce the exact
            None-becomes-empty-becomes-green defect this module exists to
            eliminate — callers MUST treat a raised exception as "the census
            could not be collected," never as "zero live groups."
    """
    # Lazy import: keep confluent-kafka off the module import path so the pure
    # handler and its unit tests never require the native client at import time.
    from confluent_kafka.admin import AdminClient

    admin = AdminClient({"bootstrap.servers": bootstrap_servers})
    future = admin.list_consumer_groups(request_timeout=_ADMIN_REQUEST_TIMEOUT_S)
    listing = future.result(timeout=_ADMIN_RESULT_TIMEOUT_S)

    errors = getattr(listing, "errors", None) or []
    if errors:
        context = ModelInfraErrorContext.with_correlation(
            transport_type=EnumInfraTransportType.KAFKA,
            operation="list_consumer_groups",
        )
        raise InfraConnectionError(
            f"list_consumer_groups against {bootstrap_servers} returned "
            f"{len(errors)} broker-level error(s); the consumer-group census is "
            "incomplete and cannot be trusted (fail-closed, OMN-14528)",
            context=context,
        )

    live: set[str] = set()
    for group in getattr(listing, "valid", None) or []:
        group_id = str(getattr(group, "group_id", ""))
        if not group_id:
            continue
        if _state_name(getattr(group, "state", "UNKNOWN")) in _NON_LIVE_STATES:
            # Empty/dead/unknown: group exists but no consumer is attached.
            continue
        live.add(group_id)

    _log.info(
        "consumer-group census: %d live group(s) on %s",
        len(live),
        bootstrap_servers,
    )
    return sorted(live)
