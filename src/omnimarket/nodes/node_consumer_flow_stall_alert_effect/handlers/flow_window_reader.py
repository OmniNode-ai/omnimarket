# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The node's own read of the consumer-flow projection (OMN-16778, redesign).

The operator-approved redesign moves assembly inside this node: the handler no
longer waits for a caller to hand it a window history, it goes and reads one.
This module is that read, and nothing else — no decision logic lives here, so
the SQL surface and the verdict surface cannot drift into disagreeing about
what ``STALLED`` means.

Two properties are load-bearing.

**It reads the projection, never the broker.**  There is no consumer, no
``rpk``, no high-watermark lookup and no offset arithmetic anywhere in this
node, and ``test_the_node_never_reads_a_broker_watermark`` asserts that
mechanically.  The reason is a premise that was falsified live on 2026-08-29:
``node_gateway_link_health_projection_compute`` delivers its intent IN-PROCESS
through ``IntentEffectDispatchBridge``, so its Kafka out-topic sits at
high-watermark 0 while the node is fully alive.  An empty out-topic is not
evidence of a stalled leg.  The projection's counters *do* see that in-process
delivery, which is exactly why the flow verdict has to come from the window
row and never from a topic depth.

**It dials the identity the topology declared.**  The DSN comes from the
contract's ``windows_source.dsn_env`` — ``OMNINODE_INTERNAL_DB_URL``, the
``omninode_runtime`` principal that owns ``omninode_internal`` — not from this
package's ambient default.  OMN-16911 is the recent, expensive precedent for
getting that wrong.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Protocol, runtime_checkable

from omnimarket.models.enum_consumer_flow_state import EnumConsumerFlowState
from omnimarket.nodes.node_consumer_flow_stall_alert_effect.models import (
    ModelFlowWindowObservation,
    ModelWindowsSource,
    WindowsSourceError,
)

logger = logging.getLogger(__name__)

#: Columns read back, in the order the SELECT returns them. Only the fields the
#: verdict and the alert payload actually use — a read that pulls a column
#: nobody consumes is a column that silently becomes load-bearing later.
_COLUMNS: tuple[str, ...] = (
    "window_start",
    "window_end",
    "flow_state",
    "messages_in",
    "messages_out",
    "messages_dlq",
    "handler_errors",
)


@runtime_checkable
class ProtocolFlowWindowReader(Protocol):
    """Structural protocol for the trailing-history read.

    Satisfied by :class:`ConsumerFlowWindowReader` and by the in-memory fakes
    the hermetic tests drive the real handler with. The handler depends on this
    and never on psycopg2, so every decision test exercises the shipped
    decision code rather than a stand-in for it.
    """

    def read_history(
        self, *, consumer_group: str, topic: str, limit: int
    ) -> tuple[ModelFlowWindowObservation, ...]:
        """Return up to ``limit`` trailing windows, OLDEST first."""
        raise NotImplementedError


class ConsumerFlowWindowReader:
    """Read the trailing window history for one (consumer_group, topic).

    Synchronous on purpose: the runtime dispatches this node's ``handle`` as a
    sync def-B handler, and opening an event loop per message is the defect
    OMN-16874 spent a lane on (``RuntimeError: Event loop is closed``, 34 of
    them, zero rows written). psycopg2 is already this repo's sync projection
    driver.

    The connection is lazy and re-used; a driver error closes it so the next
    read reconnects rather than inheriting a broken handle.
    """

    def __init__(self, source: ModelWindowsSource, *, dsn: str) -> None:
        """Bind the reader to one relation and one workload DSN.

        Args:
            source: The contract-declared read target.
            dsn: The resolved DSN for ``source.binding_ref``. Passed in rather
                than read here so the environment lookup happens at one place
                the tests can see (:func:`resolve_windows_source_dsn`).

        Raises:
            WindowsSourceError: The relation is not a plain ``schema.table``
                identifier pair, or the DSN is empty.
        """
        self._relation = source.validate_relation()
        if not dsn.strip():
            raise WindowsSourceError(
                f"windows_source DSN for binding {source.binding_ref!r} is empty; "
                "this node reads a schema whose grants are held by that binding "
                "and has no other identity to fall back on"
            )
        self._dsn = dsn
        self._conn: Any | None = None

    @property
    def relation(self) -> str:
        """The relation this reader selects from."""
        return self._relation

    def _connect(self) -> Any:
        if self._conn is not None and getattr(self._conn, "closed", 0) == 0:
            return self._conn
        import psycopg2  # type: ignore[import-untyped]

        self._conn = psycopg2.connect(self._dsn)  # no-contract-check: effect boundary
        return self._conn

    def close(self) -> None:
        """Close the connection if one is open."""
        if self._conn is not None:
            with_close = self._conn
            self._conn = None
            with_close.close()

    def read_history(
        self, *, consumer_group: str, topic: str, limit: int
    ) -> tuple[ModelFlowWindowObservation, ...]:
        """Return up to ``limit`` trailing windows for one key, OLDEST first.

        Ordered by ``window_start DESC`` in SQL (so the newest ``limit`` rows
        are the ones read) and reversed here, because the decision walks the
        history from the newest end backwards and the request model documents
        oldest-first ordering.

        Args:
            consumer_group: The consumer group to read.
            topic: The topic to read.
            limit: How many trailing windows to read.

        Returns:
            The window observations, oldest first. Empty when the projection
            holds no row for this key — a real answer, not a failure.
        """
        sql = (
            f"SELECT {', '.join(_COLUMNS)} FROM {self._relation} "
            "WHERE consumer_group = %s AND topic = %s "
            "ORDER BY window_start DESC LIMIT %s"
        )
        try:
            conn = self._connect()
            with conn.cursor() as cursor:
                cursor.execute(sql, (consumer_group, topic, limit))
                rows = cursor.fetchall()
            conn.commit()
        except Exception:
            self.close()
            raise
        return tuple(reversed([_observation(row) for row in rows]))


def _observation(row: tuple[Any, ...]) -> ModelFlowWindowObservation:
    """Build one observation from a raw result row.

    The counters are passed through as-is. An ``UNKNOWN`` window stores NULL
    counters and they stay ``None`` here — coercing them to zero would let an
    unobserved window read as observed-and-idle, which is the false-green
    OMN-16777 exists to close.
    """
    window_start, window_end, flow_state, m_in, m_out, m_dlq, errors = row
    return ModelFlowWindowObservation(
        window_start=_as_datetime(window_start, "window_start"),
        window_end=_as_datetime(window_end, "window_end"),
        flow_state=EnumConsumerFlowState(flow_state),
        messages_in=_as_optional_int(m_in),
        messages_out=_as_optional_int(m_out),
        messages_dlq=_as_optional_int(m_dlq),
        handler_errors=_as_optional_int(errors),
    )


def _as_datetime(value: object, column: str) -> datetime:
    if isinstance(value, datetime):
        return value
    raise WindowsSourceError(
        f"{column} came back as {type(value).__name__}, not a timestamp; the "
        "window history cannot be ordered without one"
    )


def _as_optional_int(value: object) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool):
        raise WindowsSourceError("flow counters must be integers, not booleans")
    if isinstance(value, int):
        return value
    raise WindowsSourceError(
        f"flow counter came back as {type(value).__name__}, not an integer or NULL"
    )


def resolve_windows_source_dsn(source: ModelWindowsSource) -> str:
    """Read the contract-named DSN out of the environment, or fail closed.

    Args:
        source: The contract-declared read target, which names the variable.

    Returns:
        The DSN string.

    Raises:
        WindowsSourceError: The named variable is unset or empty. There is no
            fallback by design — the fallback is precisely what OMN-16911 had
            to remove from the sibling projection writer.
    """
    import os

    dsn = os.environ.get(source.dsn_env, "")  # contract-config-ok: contract-named
    if not dsn.strip():
        raise WindowsSourceError(
            f"{source.dsn_env} is unset or empty. The stall alert reads "
            f"{source.relation} under topology binding {source.binding_ref!r}; "
            "without that DSN it cannot read the history it judges and must "
            "not guess a different login role (OMN-16911)"
        )
    return dsn


__all__ = [
    "ConsumerFlowWindowReader",
    "ProtocolFlowWindowReader",
    "resolve_windows_source_dsn",
]
