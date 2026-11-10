# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Real-backend sources and recorder of the landing snapshot step (OMN-23409).

- ControllerFactsFileSource: the facts the landing controller decided its last tick from, the one
  facts file it overwrites every tick. Until the node path ports the controller's Snapshot
  functions (to_build rows of the inventory), that file is the declared facts source; the step
  reads it each tick so no tick's facts are lost when the next tick overwrites it.
- WatcherStateWriteFactsSource: each PR's GraphQL node id and its head's failed run ids, read from
  the PR watcher's state (a projection of GitHub; no GitHub read here). A PR whose watcher head or
  CI head differs from the facts' head fills nothing; a state older than its bound is refused.
- JsonlLandingTickRecorder: appends each tick's record to a journal and reads every record back,
  so a tick's facts are recoverable after any later tick and after a restart.

The overlay names every path and bound; build_snapshot_ports wires the backend it selects.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from pydantic import ValidationError

from omnimarket.models.landing_decision import ModelLandingFacts
from omnimarket.nodes.node_pr_landing_snapshot_effect.handlers.adapter_publisher import (
    KafkaSnapshotPublisher,
    LaneBusSnapshotPublisher,
)
from omnimarket.nodes.node_pr_landing_snapshot_effect.handlers.snapshot_errors import (
    SnapshotSourceError,
)
from omnimarket.nodes.node_pr_landing_snapshot_effect.models.model_landing_snapshot import (
    ModelLandingTickRecord,
    ModelPrWriteFacts,
)
from omnimarket.nodes.node_pr_landing_snapshot_effect.models.model_snapshot_overlay import (
    EnumSnapshotBackend,
    ModelSnapshotOverlay,
)
from omnimarket.nodes.node_pr_landing_snapshot_effect.protocols.protocol_landing_snapshot import (
    ProtocolLandingSnapshotPublisher,
)


class ControllerFactsFileSource:
    """The landing controller's facts file for its last tick."""

    name = "controller-facts-file"

    def __init__(self, path: Path) -> None:
        self._path = path

    def _load(self) -> ModelLandingFacts:
        try:
            raw = self._path.read_text(encoding="utf-8")
        except OSError as exc:
            raise SnapshotSourceError(
                f"{self._path} unreadable: {type(exc).__name__}"
            ) from None
        try:
            return ModelLandingFacts.model_validate_json(raw)
        except ValidationError as exc:
            raise SnapshotSourceError(
                f"{self._path} is not landing facts: {exc.error_count()} errors, "
                f"first at {'.'.join(str(p) for p in exc.errors()[0]['loc'])}"
            ) from None

    def current_tick(self) -> int:
        """The tick the file holds now."""
        return self._load().tick

    def read_facts(self, tick: int) -> ModelLandingFacts | None:
        facts = self._load()
        return facts if facts.tick == tick else None


class WatcherStateWriteFactsSource:
    """Node ids and failed run ids from the PR watcher's state."""

    name = "pr-watcher-state"

    def __init__(
        self,
        path: Path,
        *,
        owner: str,
        max_age_seconds: int,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._path = path
        self._owner = owner
        self._max_age = max_age_seconds
        self._now = now

    def _state(self) -> dict[str, object]:
        try:
            state = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise SnapshotSourceError(
                f"{self._path} unreadable: {type(exc).__name__}"
            ) from None
        if not isinstance(state, dict) or not isinstance(state.get("prs"), dict):
            raise SnapshotSourceError(f"{self._path} holds no pr table")
        stamp = state.get("last_tick")
        try:
            at = datetime.strptime(str(stamp), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
        except ValueError:
            raise SnapshotSourceError(
                f"{self._path} last_tick {stamp!r} unreadable"
            ) from None
        age = (self._now() - at).total_seconds()
        if age > self._max_age:
            raise SnapshotSourceError(
                f"{self._path} is {int(age)}s old, past its bound of {self._max_age}s"
            )
        return state

    def read_write_facts(self, heads: dict[str, str]) -> dict[str, ModelPrWriteFacts]:
        prs = self._state()["prs"]
        if not isinstance(prs, dict):
            raise SnapshotSourceError(f"{self._path} holds no pr table")
        out: dict[str, ModelPrWriteFacts] = {}
        prefix = f"{self._owner}/"
        for key, head in heads.items():
            if not key.startswith(prefix):
                continue
            entry = prs.get(key[len(prefix) :])
            if not isinstance(entry, dict):
                continue
            facts = entry.get("facts") or {}
            if not isinstance(facts, dict) or facts.get("head_sha") != head:
                continue
            node_id = facts.get("node_id")
            ci = entry.get("ci") or {}
            runs: list[int] = []
            if isinstance(ci, dict) and ci.get("sha") == head:
                for row in ci.get("red_facts") or []:
                    if isinstance(row, list) and len(row) > 2 and str(row[2]).isdigit():
                        run = int(row[2])
                        if run not in runs:
                            runs.append(run)
            out[key] = ModelPrWriteFacts(
                pr=key,
                head_sha=head,
                pr_node_id=str(node_id) if node_id else None,
                failed_run_ids=tuple(runs),
            )
        return out


class JsonlLandingTickRecorder:
    """Appends every tick's record to a journal; reads them all back."""

    def __init__(self, path: Path) -> None:
        self._path = path

    def record(self, record: ModelLandingTickRecord) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._path.open("a", encoding="utf-8") as fh:
            fh.write(record.model_dump_json() + "\n")

    def recorded(self) -> tuple[ModelLandingTickRecord, ...]:
        if not self._path.exists():
            return ()
        return tuple(
            ModelLandingTickRecord.model_validate_json(line)
            for line in self._path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        )


def resolve_path(value: str) -> Path:
    return Path(os.path.expanduser(os.path.expandvars(value)))


def build_snapshot_ports(
    overlay: ModelSnapshotOverlay,
    *,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
    publisher: ProtocolLandingSnapshotPublisher | None = None,
) -> tuple[
    ControllerFactsFileSource,
    WatcherStateWriteFactsSource,
    JsonlLandingTickRecorder,
    ProtocolLandingSnapshotPublisher,
]:
    """The redpanda profile's ports, every path and bound from the overlay; the clock and publisher injectable."""
    if overlay.backend is not EnumSnapshotBackend.REDPANDA:
        raise ValueError("the in-memory backend's ports are injected, not built")
    if not (
        overlay.facts_path
        and overlay.watcher_state_path
        and overlay.watcher_max_age_seconds
        and overlay.owner
        and overlay.record_journal
    ):
        raise ValueError("the redpanda backend needs its overlay fields")
    return (
        ControllerFactsFileSource(resolve_path(overlay.facts_path)),
        WatcherStateWriteFactsSource(
            resolve_path(overlay.watcher_state_path),
            owner=overlay.owner,
            max_age_seconds=overlay.watcher_max_age_seconds,
            now=now,
        ),
        JsonlLandingTickRecorder(resolve_path(overlay.record_journal)),
        publisher or _publisher(overlay),
    )


def _publisher(overlay: ModelSnapshotOverlay) -> ProtocolLandingSnapshotPublisher:
    if overlay.bus_lane and overlay.workspace_root:
        return LaneBusSnapshotPublisher(
            overlay.bus_lane, resolve_path(overlay.workspace_root)
        )
    if overlay.bootstrap_servers:
        return KafkaSnapshotPublisher(overlay.bootstrap_servers)
    raise ValueError("the redpanda backend names one of bus_lane or bootstrap_servers")


__all__: list[str] = [
    "ControllerFactsFileSource",
    "JsonlLandingTickRecorder",
    "SnapshotSourceError",
    "WatcherStateWriteFactsSource",
    "build_snapshot_ports",
    "resolve_path",
]
