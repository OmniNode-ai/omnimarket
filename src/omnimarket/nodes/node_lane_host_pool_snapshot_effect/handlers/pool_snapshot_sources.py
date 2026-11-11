# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Real-backend sources for the lane host pool snapshot step (OMN-23664).

- AdvertisementFileSource / AdvertisementCommandSource: host capacity advertisements from a file or a command.
- LabHostTableSource: the lab host table from the overlay environment.
- LedgerRunningLanesSource: live lane counts per host from the ledger claims.
- TokenPoolCommandAccountsSource: per-host token pool accounts via the token pool command.

The overlay names every path and bound; build_pool_snapshot_ports wires the backend it selects.
"""

from __future__ import annotations

import json
import os
import subprocess
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path
from uuid import uuid4

from pydantic import ValidationError

from omnimarket.handlers.node_overlay_reader import NodeOverlayError
from omnimarket.handlers.overlay_keys import resolve_lab_hosts_table
from omnimarket.models.model_host_capacity_advertisement import (
    ModelHostCapacityAdvertisement,
)
from omnimarket.models.model_reading import ModelLedgerEvidenceRow
from omnimarket.models.overlay_keys import ModelLabHostRow
from omnimarket.nodes.node_lane_host_pool_snapshot_effect.handlers.snapshot_errors import (
    PoolSnapshotSourceError,
)
from omnimarket.nodes.node_lane_host_pool_snapshot_effect.models.model_pool_snapshot import (
    ModelHostAccounts,
    ModelTokenPoolStatusReading,
)
from omnimarket.nodes.node_lane_host_pool_snapshot_effect.models.model_pool_snapshot_overlay import (
    ModelAccountsRoute,
)
from omnimarket.nodes.node_reading_writer_effect.protocols import (
    ModelLedgerQuery,
    ProtocolLedgerRowSource,
)


def parse_advertisements(text: str) -> tuple[ModelHostCapacityAdvertisement, ...]:
    """Parse a JSON list of host capacity advertisements; skip rows that fail validation."""
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, ValueError) as exc:
        raise PoolSnapshotSourceError(f"advertisements not JSON: {exc}") from None
    if not isinstance(data, list):
        raise PoolSnapshotSourceError("advertisements must be a JSON list")
    fields = ModelHostCapacityAdvertisement.model_fields
    out: list[ModelHostCapacityAdvertisement] = []
    for row in data:
        if not isinstance(row, dict):
            continue
        filtered = {k: v for k, v in row.items() if k in fields}
        try:
            out.append(ModelHostCapacityAdvertisement.model_validate(filtered))
        except ValidationError:
            continue
    return tuple(out)


class AdvertisementFileSource:
    """Host capacity advertisements from a JSON file."""

    name = "advertisements-file"

    def __init__(self, path: Path) -> None:
        self._path = path

    def read_advertisements(self) -> tuple[ModelHostCapacityAdvertisement, ...]:
        try:
            text = self._path.read_text(encoding="utf-8")
        except OSError as exc:
            raise PoolSnapshotSourceError(
                f"{self._path} unreadable: {type(exc).__name__}"
            ) from None
        return parse_advertisements(text)


def _default_runner(argv: Sequence[str], timeout_seconds: int) -> tuple[int, str]:
    """Run argv with no shell: the return code and stdout when it passed, else stderr."""
    try:
        proc = subprocess.run(
            list(argv),
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
        )
        return proc.returncode, proc.stdout if proc.returncode == 0 else proc.stderr
    except subprocess.TimeoutExpired:
        raise PoolSnapshotSourceError(
            f"command timed out after {timeout_seconds}s: {argv[0]}"
        ) from None
    except OSError as exc:
        raise PoolSnapshotSourceError(
            f"command not run ({type(exc).__name__}): {argv[0]}"
        ) from None


class AdvertisementCommandSource:
    """Host capacity advertisements from a command's stdout."""

    name = "advertisements-command"

    def __init__(
        self,
        argv: Sequence[str],
        timeout_seconds: int,
        runner: Callable[[Sequence[str], int], tuple[int, str]] | None = None,
    ) -> None:
        self._argv = tuple(argv)
        self._timeout = timeout_seconds
        self._runner = runner or _default_runner

    def read_advertisements(self) -> tuple[ModelHostCapacityAdvertisement, ...]:
        rc, text = self._runner(self._argv, self._timeout)
        if rc != 0:
            tail = text.strip().splitlines()[-1] if text.strip() else ""
            raise PoolSnapshotSourceError(
                f"advertisement command rc={rc}: {self._argv[0]} stderr tail: {tail}"
            )
        return parse_advertisements(text)


class LabHostTableSource:
    """The lab host table from the overlay environment."""

    name = "host-table"

    def __init__(self, environ: Mapping[str, str]) -> None:
        self._environ = environ

    def read_hosts(self) -> tuple[ModelLabHostRow, ...]:
        try:
            table = resolve_lab_hosts_table(self._environ)
        except NodeOverlayError as exc:
            raise PoolSnapshotSourceError(f"host table unreadable: {exc}") from None
        return table.hosts


class LedgerRunningLanesSource:
    """Open lane counts per host from the ledger's CLAIM rows, read through the reading writer's row source.

    A lane runs on a host while a CLAIM row carrying ``host=<name>`` is inside the lookback and has no later TERMINAL or RELEASE
    row of its lane. Rows older than the lookback are not read, so a claim that old counts as no lane.
    """

    name = "ledger-running-lanes"

    def __init__(self, source: ProtocolLedgerRowSource, lookback_hours: float) -> None:
        self._source = source
        self._lookback = timedelta(hours=lookback_hours)

    def _rows(self, row_type: str, now: datetime) -> tuple[ModelLedgerEvidenceRow, ...]:
        read = self._source.read(
            ModelLedgerQuery(
                since=now - self._lookback,
                until=now + timedelta(days=1),
                row_type=row_type,
                # The bus read command refuses a query with no correlation id.
                correlation_id=str(uuid4()),
            )
        )
        if read.error is not None:
            raise PoolSnapshotSourceError(
                f"ledger {row_type} rows unread: {read.error}"
            )
        return read.rows

    def running_lanes(self, now: datetime) -> dict[str, int]:
        ends: dict[str, datetime] = {}
        for end_type in ("TERMINAL", "RELEASE"):
            for row in self._rows(end_type, now):
                if row.lane is not None:
                    ends[row.lane] = max(ends.get(row.lane, row.stamp), row.stamp)
        counts: dict[str, int] = {}
        counted: set[str] = set()
        for claim in self._rows("CLAIM", now):
            host = claim.cells.get("host")
            lane = claim.lane
            if not host or lane is None or lane in counted:
                continue
            if lane in ends and ends[lane] >= claim.stamp:
                continue
            counted.add(lane)
            counts[host] = counts.get(host, 0) + 1
        return counts


class TokenPoolCommandAccountsSource:
    """Per-host token pool accounts via the token pool status command."""

    name = "accounts-token-pool"

    def __init__(
        self,
        route: ModelAccountsRoute,
        runner: Callable[[Sequence[str], int], tuple[int, str]] | None = None,
        *,
        registry_roots: Sequence[str] = (),
    ) -> None:
        self._route = route
        self._runner = runner or _default_runner
        self._registry_roots = tuple(registry_roots)

    def read_accounts(
        self, hosts: tuple[ModelLabHostRow, ...]
    ) -> tuple[ModelHostAccounts, ...]:
        """One reading per host, in the order given. The hosts are read at once: each far read is its own ssh run, and one run that
        waits out its timeout must not hold the rest past the tick's bound."""
        if not hosts:
            return ()
        roots = " ".join(f'"{root}"' for root in self._registry_roots)
        with ThreadPoolExecutor(max_workers=len(hosts)) as pool:
            return tuple(pool.map(lambda row: self._read_row(row, roots), hosts))

    def _read_row(self, row: ModelLabHostRow, roots: str) -> ModelHostAccounts:
        if not row.available:
            return ModelHostAccounts(host=row.name, unread_reason="away")
        if row.local:
            argv = [os.path.expandvars(a) for a in self._route.local_argv]
        else:
            if (
                any("{registry_roots}" in a for a in self._route.remote_argv)
                and not roots
            ):
                return ModelHostAccounts(
                    host=row.name, unread_reason="no registry root declared"
                )
            argv = [
                a.replace("{target}", row.target).replace("{registry_roots}", roots)
                for a in self._route.remote_argv
            ]
        try:
            rc, stdout = self._runner(argv, self._route.timeout_seconds)
        except PoolSnapshotSourceError as exc:
            return ModelHostAccounts(host=row.name, unread_reason=str(exc))
        if rc != 0:
            return ModelHostAccounts(
                host=row.name,
                unread_reason=f"token pool command rc={rc}: {stdout.strip()[-120:]}",
            )
        try:
            result = ModelTokenPoolStatusReading.model_validate_json(stdout)
        except ValidationError:
            return ModelHostAccounts(
                host=row.name, unread_reason="token pool output not JSON"
            )
        usable: list[str] = []
        limited: list[str] = []
        for token in result.tokens:
            if token.limited or token.revoked:
                limited.append(token.label)
            else:
                usable.append(token.label)
        return ModelHostAccounts(
            host=row.name, usable=tuple(usable), limited=tuple(limited)
        )


def resolve_path(value: str) -> Path:
    return Path(os.path.expanduser(os.path.expandvars(value)))
