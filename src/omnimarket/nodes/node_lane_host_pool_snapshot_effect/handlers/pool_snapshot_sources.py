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
import re
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
from omnimarket.models.model_point_query import ModelPointQuery
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
from omnimarket.nodes.node_work_ledger_reader_effect.protocols import (
    ProtocolLedgerPointRead,
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


# What ssh returns when it could not reach the destination (not the remote command's own code).
SSH_CONNECT_FAILED_RC = 255


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


_OPEN_CLAIM_LINE = re.compile(r"^OPEN-CLAIM lane=(?P<lane>\S+) .*\bhost=(?P<host>\S+)")


class LedgerOpenClaimsLanesSource:
    """Open lane counts per host from the ledger node's open-claims answer, one point query read through the bus read command.

    The node answers the open set from its state table with the claim's host cell, so the question is one small call and not the
    TERMINAL, RELEASE and CLAIM rows of the whole lookback (about two thousand rows, over the node's answer bound and past the read
    time limit on 2026-11-14). A lane counts once; a claim with no host names no host. The answer's next cursor is followed to the end.
    """

    name = "ledger-open-claims"

    def __init__(self, read: ProtocolLedgerPointRead, lookback_hours: float) -> None:
        self._read = read
        self._lookback = timedelta(hours=lookback_hours)

    def running_lanes(self, now: datetime) -> dict[str, int]:
        since = now - self._lookback
        counts: dict[str, int] = {}
        counted: set[str] = set()
        cursor: str | None = None
        while True:
            answer = self._read.read(
                ModelPointQuery(query="open-claims", claims_since=since, cursor=cursor)
            )
            if answer.error is not None:
                raise PoolSnapshotSourceError(
                    f"ledger open claims unread: {answer.error}"
                )
            for line in answer.lines:
                found = _OPEN_CLAIM_LINE.match(line)
                if found is None or found["host"] == "none" or found["lane"] in counted:
                    continue
                counted.add(found["lane"])
                counts[found["host"]] = counts.get(found["host"], 0) + 1
            if answer.next_cursor is None:
                return counts
            if answer.next_cursor == cursor:
                raise PoolSnapshotSourceError(
                    "ledger open claims unread: the next cursor did not advance"
                )
            cursor = answer.next_cursor


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
            return self._read_destination(row, argv)[1]
        if any("{registry_roots}" in a for a in self._route.remote_argv) and not roots:
            return ModelHostAccounts(
                host=row.name, unread_reason="no registry root declared"
            )
        # The row's destinations in the order it declares them: ssh ends 255 when a name does not resolve or a login is
        # refused, and only then does the next destination of the same machine get its turn.
        destinations = (row.target, *row.fallback_targets)
        for destination in destinations[:-1]:
            rc, accounts = self._read_destination(
                row, self._remote_argv(destination, roots)
            )
            if rc != SSH_CONNECT_FAILED_RC:
                return accounts
        return self._read_destination(row, self._remote_argv(destinations[-1], roots))[
            1
        ]

    def _remote_argv(self, destination: str, roots: str) -> list[str]:
        return [
            a.replace("{target}", destination).replace("{registry_roots}", roots)
            for a in self._route.remote_argv
        ]

    def _read_destination(
        self, row: ModelLabHostRow, argv: Sequence[str]
    ) -> tuple[int | None, ModelHostAccounts]:
        """The run's return code (None when the run never ended in one) and the reading it gave."""
        try:
            rc, stdout = self._runner(argv, self._route.timeout_seconds)
        except PoolSnapshotSourceError as exc:
            return None, ModelHostAccounts(host=row.name, unread_reason=str(exc))
        if rc != 0:
            return rc, ModelHostAccounts(
                host=row.name,
                unread_reason=f"token pool command rc={rc}: {stdout.strip()[-120:]}",
            )
        return rc, self._accounts_from_status(row, stdout)

    def _accounts_from_status(
        self, row: ModelLabHostRow, stdout: str
    ) -> ModelHostAccounts:
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
