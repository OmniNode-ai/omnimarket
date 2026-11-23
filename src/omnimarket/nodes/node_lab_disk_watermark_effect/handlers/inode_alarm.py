# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Inode alarm of the lab disk watermark: a volume below the declared free share of inodes writes the reaper's alarm rows.

A filesystem out of inodes fails every write with ENOSPC while the byte share reads healthy. On 2026-11-23 the root
filesystem of h053 read 94 to 95 percent of inodes used while the watermark, which reads bytes only, stayed at ``warn``.
The alarm uses the route the lab stack reaper uses: one FRICTION row per alarm and one STATUS row with ``alarms=<n>``,
appended through the bus append command (OMN-24562). The alarm names its owner in both rows.
"""

from __future__ import annotations

import json
import os
import shlex
import uuid
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import NamedTuple

import yaml

from omnimarket.nodes.node_lab_disk_watermark_effect.models.model_lab_disk_watermark import (
    ModelInodeAlarm,
    ModelInodeAlarmDeclaration,
    ModelInodeReading,
)
from omnimarket.nodes.node_reading_writer_effect.adapters.ledger_adapters import (
    CommandLedgerAppender,
)
from omnimarket.nodes.node_reading_writer_effect.protocols import (
    ProtocolReadingAppender,
)

CONTRACT = Path(__file__).resolve().parent.parent / "contract.yaml"
STAMP_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
ACTOR = "script:onex-disk-watermark"


class InodeUsage(NamedTuple):
    files: int
    free: int


def inode_usage(path: str) -> InodeUsage:
    """The inodes of the filesystem holding ``path``, as ``df -i`` counts them (total and free)."""
    stat = os.statvfs(path)
    return InodeUsage(stat.f_files, stat.f_ffree)


def load_inode_alarm() -> ModelInodeAlarmDeclaration:
    """The alarm the contract declares; a contract with none refuses, there is no default share or owner."""
    document = yaml.safe_load(CONTRACT.read_text(encoding="utf-8"))
    return ModelInodeAlarmDeclaration(**document["inode_alarm"])


def free_percent(usage: InodeUsage) -> float | None:
    """Free inodes over all inodes in percent; None for a filesystem that counts no inodes (it has nothing to run out of)."""
    if usage.files <= 0:
        return None
    return 100.0 * usage.free / usage.files


def read_inodes(
    paths: tuple[str, ...],
    declaration: ModelInodeAlarmDeclaration,
    usage: Callable[[str], InodeUsage] = inode_usage,
) -> tuple[tuple[ModelInodeReading, ...], tuple[str, ...]]:
    """The inode reading of each volume, and the volumes that could not be read (named, never skipped silently)."""
    readings: list[ModelInodeReading] = []
    unread: list[str] = []
    for path in paths:
        try:
            counted = usage(path)
        except OSError:
            unread.append(path)
            continue
        share = free_percent(counted)
        if share is None:
            continue
        readings.append(
            ModelInodeReading(
                path=path,
                files=counted.files,
                free=counted.free,
                free_percent=round(share, 2),
                alarm=share < declaration.free_percent_below,
            )
        )
    return tuple(readings), tuple(unread)


def alarm_rows(
    host_name: str,
    reading: ModelInodeReading,
    declaration: ModelInodeAlarmDeclaration,
    now: datetime,
) -> tuple[str, str]:
    """The reaper's two rows for one alarm: the FRICTION row and the STATUS row with ``alarms=1``, both naming the owner."""
    stamp = now.strftime(STAMP_FORMAT)
    text = (
        f"{reading.path} on {host_name} has {reading.free} of {reading.files} inodes free "
        f"({reading.free_percent}%), below the {declaration.free_percent_below:g}% alarm share"
    )
    friction = (
        f"{stamp} | FRICTION | lane={declaration.lane} | ticket={declaration.ticket}"
        f" | cost=est ~0.5 lane-hours; a filesystem out of inodes refuses every lane write and placement; ({declaration.ticket})"
        f" | owner={declaration.owner} | host={host_name} | {text}"
    )
    status = (
        f"{stamp} | STATUS | lane={declaration.lane} | ticket={declaration.ticket}"
        f" | actor={ACTOR} | model=none | action=inode-watermark | decision=alarmed"
        f" | owner={declaration.owner} | host={host_name} | {text}; alarms=1"
    )
    return friction, status


def build_appender(
    declaration: ModelInodeAlarmDeclaration,
    host_name: str,
    environ: Mapping[str, str],
) -> ProtocolReadingAppender | None:
    """The bus append command the declaration names, or None when the environment does not set it."""
    argv = shlex.split(environ.get(declaration.ledger_append_command_env, ""))
    if not argv:
        return None
    return CommandLedgerAppender(
        argv, declaration.lane, host_name, declaration.ledger_append_timeout_seconds
    )


def _recent(state_file: Path, path: str, now: datetime, minutes: int) -> bool:
    """True when this volume alarmed within ``minutes``: one alarm per volume per interval, not one per tick."""
    try:
        document = json.loads(state_file.read_text(encoding="utf-8"))
        last = datetime.strptime(document[path], STAMP_FORMAT).replace(tzinfo=UTC)
    except (OSError, ValueError, KeyError, TypeError):
        return False
    return timedelta(0) <= now - last < timedelta(minutes=minutes)


def _remember(state_file: Path, path: str, now: datetime) -> None:
    try:
        document = json.loads(state_file.read_text(encoding="utf-8"))
        if not isinstance(document, dict):
            document = {}
    except (OSError, ValueError):
        document = {}
    document[path] = now.strftime(STAMP_FORMAT)
    state_file.parent.mkdir(parents=True, exist_ok=True)
    temporary = state_file.with_name(f".{state_file.name}.tmp")
    temporary.write_text(json.dumps(document), encoding="utf-8")
    os.replace(temporary, state_file)


def raise_alarms(
    host_name: str,
    readings: tuple[ModelInodeReading, ...],
    declaration: ModelInodeAlarmDeclaration,
    *,
    now: datetime,
    appender: ProtocolReadingAppender | None,
    state_file: Path,
    persist: bool,
    new_id: Callable[[], str] = lambda: uuid.uuid4().hex,
) -> tuple[ModelInodeAlarm, ...]:
    """One alarm per volume below the declared share; its rows are appended once per ``repeat_minutes``.

    An alarm that could not be written (no append command in the environment, a refused or failed append) is still
    returned with ``written=False`` and the reason in ``note``, so the run still fails loudly: the alarm never turns
    into silence because its route is missing.
    """
    alarms: list[ModelInodeAlarm] = []
    for reading in readings:
        if not reading.alarm:
            continue
        rows = alarm_rows(host_name, reading, declaration, now)
        written = False
        note = ""
        if not persist:
            note = "dry run: rows not appended"
        elif _recent(state_file, reading.path, now, declaration.repeat_minutes):
            note = f"already alarmed within {declaration.repeat_minutes} minutes"
        elif appender is None:
            note = f"{declaration.ledger_append_command_env} is not set: the alarm rows have no ledger route"
        else:
            outcome = appender.append("\n".join(rows) + "\n", new_id())
            written = outcome.ok
            note = (
                ""
                if outcome.ok
                else f"append {outcome.status}: {outcome.message[:200]}"
            )
            if outcome.ok:
                _remember(state_file, reading.path, now)
        alarms.append(
            ModelInodeAlarm(
                host_name=host_name,
                path=reading.path,
                free_percent=reading.free_percent,
                free=reading.free,
                files=reading.files,
                threshold_percent=declaration.free_percent_below,
                owner=declaration.owner,
                rows=rows,
                written=written,
                note=note,
            )
        )
    return tuple(alarms)
