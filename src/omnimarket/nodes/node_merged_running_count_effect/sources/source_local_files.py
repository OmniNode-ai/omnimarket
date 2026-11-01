# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The count's sources read from the files a lab host holds: the PR watcher state, the work ledger, the placement read, the host table."""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field

from omnimarket.nodes.node_merged_running_count_compute.models.model_merged_running_count import (
    ModelMrcEvidence,
    ModelMrcHost,
    ModelMrcLiveLane,
    ModelMrcPr,
)

_ADOPTED = re.compile(
    r"^(?P<ts>\S+) \| STATUS \| lane=(?P<lane>[^ |]+) .*?\bADOPTED pr=(?P<repo>[^#\s]+)#(?P<number>\d+) merge=(?P<sha>[0-9a-f]+)"
)


class ModelSourceLabels(BaseModel):
    """The words the files use and the labels the count's pull request states take, from the effect's contract."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    adopt_lane: str = Field(min_length=1)
    adopted_kind: str = Field(min_length=1)
    merged_state: str = Field(min_length=1)
    handed_off_state: str = Field(min_length=1)
    armed_state: str = Field(min_length=1)
    queued_state: str = Field(min_length=1)
    open_state: str = Field(min_length=1)
    claim_row: str = Field(min_length=1)
    terminal_row: str = Field(min_length=1)
    lane_live_window_hours: int = Field(gt=0)


def _cells(line: str) -> dict[str, str]:
    cells: dict[str, str] = {}
    for cell in line.split(" | "):
        key, sep, value = cell.partition("=")
        if sep and " " not in key:
            cells.setdefault(key.strip(), value.strip())
    return cells


def _utc(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


class SourceLocalFiles:
    """Implements ProtocolMergedRunningSources over local files; each path is given by the caller from the effect's contract."""

    def __init__(
        self,
        *,
        watcher_state: Path,
        ledger: Path,
        labels: ModelSourceLabels,
        hosts_table: Path,
        placement_read: Path,
        now: datetime,
    ) -> None:
        self._watcher_state = watcher_state
        self._ledger = ledger
        self._labels = labels
        self._hosts_table = hosts_table
        self._placement_read = placement_read
        self._now = now

    def read_prs(self) -> tuple[ModelMrcPr, ...]:
        state: dict[str, Any] = json.loads(
            self._watcher_state.read_text(encoding="utf-8")
        )
        rows = [
            ModelMrcPr(
                repo=str(m["repo"]),
                number=int(m["number"]),
                state=self._labels.merged_state,
                base_branch=str(m.get("base") or ""),
                merge_sha=str(m.get("merge_sha") or ""),
                merged_at=_utc(m["merged_at"]),
            )
            for m in state["merges"].values()
            if m.get("merged_at")
        ]
        for pr in state["prs"].values():
            facts = pr.get("facts") or {}
            if str(facts.get("state", "")).lower() == self._labels.merged_state:
                continue
            if pr.get("handoff"):
                label = self._labels.handed_off_state
            elif facts.get("armed"):
                label = self._labels.armed_state
            elif pr.get("queued"):
                label = self._labels.queued_state
            else:
                label = self._labels.open_state
            rows.append(
                ModelMrcPr(
                    repo=str(facts["repo"]),
                    number=int(facts["number"]),
                    state=label,
                    base_branch=str(facts.get("base") or ""),
                )
            )
        return tuple(rows)

    def read_evidence(self) -> tuple[ModelMrcEvidence, ...]:
        found: list[ModelMrcEvidence] = []
        for line in self._ledger.read_text(encoding="utf-8").splitlines():
            match = _ADOPTED.match(line)
            if match is None or match["lane"] != self._labels.adopt_lane:
                continue
            found.append(
                ModelMrcEvidence(
                    repo=match["repo"],
                    kind=self._labels.adopted_kind,
                    source=f"STATUS lane={match['lane']}",
                    number=int(match["number"]),
                    merge_sha=match["sha"],
                    observed_at=_utc(match["ts"]),
                )
            )
        return tuple(found)

    def read_hosts(self) -> tuple[ModelMrcHost, ...]:
        table = yaml.safe_load(self._hosts_table.read_text(encoding="utf-8"))["hosts"]
        advertised: dict[str, int] = {}
        if self._placement_read.exists():
            for ad in json.loads(self._placement_read.read_text(encoding="utf-8")):
                advertised[str(ad["host_name"])] = int(ad["max_units"])
        return tuple(
            ModelMrcHost(
                host=str(row["name"]),
                slots=advertised.get(str(row["name"]), row.get("cap")),
                available=row.get("available") is not False,
            )
            for row in table
        )

    def read_live_lanes(self) -> tuple[ModelMrcLiveLane, ...]:
        """Lanes with a CLAIM naming a host, no later TERMINAL, claimed inside the live window."""
        claimed: dict[str, tuple[datetime, str]] = {}
        for line in self._ledger.read_text(encoding="utf-8").splitlines():
            ts, _, rest = line.partition(" | ")
            kind, _, tail = rest.partition(" | ")
            if kind not in (self._labels.claim_row, self._labels.terminal_row):
                continue
            cells = _cells(tail)
            lane = cells.get("lane", "")
            if not lane:
                continue
            if kind == self._labels.terminal_row:
                claimed.pop(lane, None)
            elif cells.get("host"):
                claimed[lane] = (_utc(ts), cells["host"])
        horizon = self._labels.lane_live_window_hours * 3600
        return tuple(
            ModelMrcLiveLane(lane=lane, host=host)
            for lane, (at, host) in sorted(claimed.items())
            if (self._now - at).total_seconds() <= horizon
        )
