# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The ports of one scan, made from the node's settings: the runs, the changes, the live claims, the seen record and the bus."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from omnimarket.nodes.node_delegation_check_regression_effect.adapters.bus_publisher import (
    BusRegressionPublisher,
)
from omnimarket.nodes.node_delegation_check_regression_effect.adapters.change_sources import (
    CompositeChangeSource,
    GitMergeChangeSource,
    LedgerMarkerChangeSource,
)
from omnimarket.nodes.node_delegation_check_regression_effect.adapters.claims import (
    LedgerClaimHolders,
)
from omnimarket.nodes.node_delegation_check_regression_effect.adapters.gateway_runs import (
    GatewayProbeRunSource,
)
from omnimarket.nodes.node_delegation_check_regression_effect.adapters.inmemory_adapters import (
    InMemorySeenStore,
)
from omnimarket.nodes.node_delegation_check_regression_effect.adapters.state_files import (
    FileDevBuildStore,
    FileSeenStore,
)
from omnimarket.nodes.node_delegation_check_regression_effect.models.model_regression import (
    ModelProbeRun,
    ModelRegressionSettings,
    ModelWatchedCheck,
)
from omnimarket.nodes.node_delegation_check_regression_effect.protocols import (
    ProtocolChangeSource,
    ProtocolLiveClaims,
    ProtocolProbeRunSource,
    ProtocolRegressionPublisher,
    ProtocolSeenStore,
)
from omnimarket.nodes.node_reading_writer_effect.adapters.ledger_adapters import (
    build_row_source,
)


class UntilRunSource:
    """A replay clock over any run source: a run finished after ``until`` has not happened yet."""

    def __init__(self, inner: ProtocolProbeRunSource, until: datetime) -> None:
        self._inner = inner
        self._until = until

    def runs(self, check: ModelWatchedCheck, limit: int) -> tuple[ModelProbeRun, ...]:
        return tuple(r for r in self._inner.runs(check, limit) if r.at <= self._until)


@dataclass(frozen=True)
class RegressionPorts:
    runs: ProtocolProbeRunSource
    changes: ProtocolChangeSource
    claims: ProtocolLiveClaims
    seen: ProtocolSeenStore
    publisher: ProtocolRegressionPublisher


@contextmanager
def open_ports(
    settings: ModelRegressionSettings,
    *,
    now: datetime,
    registry_root: Path,
    bus: str,
    replay: bool,
) -> Iterator[RegressionPorts]:
    """The ports of one scan; the bus opened for it is closed after it. A replay reads runs only up to ``now`` and keeps no seen record."""
    rows = build_row_source(settings.ledger_read)
    changes = CompositeChangeSource(
        [
            LedgerMarkerChangeSource(rows, settings.change_markers),
            GitMergeChangeSource(registry_root, settings.merge_sources),
            FileDevBuildStore(registry_root / settings.dev_builds_relpath),
        ]
    )
    source: ProtocolProbeRunSource = GatewayProbeRunSource(
        settings.gateway_timeout_seconds
    )
    if replay:
        source = UntilRunSource(source, now)
    seen: ProtocolSeenStore = (
        InMemorySeenStore()
        if replay
        else FileSeenStore(registry_root / settings.seen_relpath)
    )
    with BusRegressionPublisher(
        "kafka" if bus == "kafka" else "inmemory", settings.bus_lane, registry_root
    ) as publisher:
        yield RegressionPorts(
            runs=source,
            changes=changes,
            claims=LedgerClaimHolders(rows, now),
            seen=seen,
            publisher=publisher,
        )
