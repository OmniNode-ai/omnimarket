# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Real-backend sources and recorder of the landing snapshot step (OMN-23409).

- LandingFactsFileSource: the facts the landing controller decided its last tick from, the one
  facts file it overwrites every tick. Until the node path ports the controller's Snapshot
  functions (to_build rows of the inventory), that file is the declared facts source; the step
  reads it each tick so no tick's facts are lost when the next tick overwrites it.
- WatcherStateWriteFactsSource: each PR's GraphQL node id and its head's failed run ids, read from
  the PR watcher's state (a projection of GitHub; no GitHub read here). A PR whose watcher head or
  CI head differs from the facts' head fills nothing; a state older than its bound is refused.
- LandingCarryStateDirSource: the old controller's last finished tick during the cutover period (OMN-23665):
  its facts file, its tick log (the tick is finished when the log's last row names it) and its decision
  log's action rows for that tick. The controller keeps no copy of its decision, only the facts it decided
  from and the actions it took, so the carry holds those two.
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
from typing import Any

from pydantic import ValidationError

from omnimarket.models.landing_controller_carry import (
    REASON_MAX_LENGTH,
    ModelLandingControllerCarry,
    ModelLandingControllerJudged,
    ModelLandingControllerLogged,
)
from omnimarket.models.landing_decision import (
    EnumLandingActionKind,
    ModelLandingExternalJudged,
    ModelLandingFacts,
)
from omnimarket.nodes.node_pr_landing_snapshot_effect.handlers.adapter_publisher import (
    KafkaSnapshotPublisher,
    LaneBusSnapshotPublisher,
)
from omnimarket.nodes.node_pr_landing_snapshot_effect.handlers.snapshot_errors import (
    ControllerStateStaleError,
    ControllerTickOpenError,
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


class LandingFactsFileSource:
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

    def read_current(self) -> ModelLandingFacts:
        """The facts the file holds now."""
        return self._load()

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


_ACTION_KINDS = frozenset(kind.value for kind in EnumLandingActionKind)
_STAMP = "%Y-%m-%dT%H:%M:%SZ"


def _tail_text(path: Path, max_bytes: int, *, missing_ok: bool) -> tuple[str, bool]:
    """The last ``max_bytes`` of a file as text and whether that is the whole file; never reads more.

    A tail that starts inside a line drops that partial line, so every returned line is whole.
    """
    try:
        with path.open("rb") as fh:
            size = fh.seek(0, 2)
            start = max(0, size - max_bytes)
            fh.seek(start)
            data = fh.read(size - start)
    except FileNotFoundError:
        if missing_ok:
            return "", True
        raise SnapshotSourceError(f"{path} unreadable: FileNotFoundError") from None
    except OSError as exc:
        raise SnapshotSourceError(f"{path} unreadable: {type(exc).__name__}") from None
    whole = start == 0
    if not whole:
        newline = data.find(b"\n")
        data = b"" if newline < 0 else data[newline + 1 :]
    return data.decode("utf-8", errors="replace"), whole


def _jsonl_tail_rows(
    path: Path, max_bytes: int, *, missing_ok: bool
) -> tuple[list[dict[str, object]], bool]:
    """The JSON objects in a jsonl file's last ``max_bytes``, one per line, and whether they are the whole file.

    A line that is not an object is skipped.
    """
    text, whole = _tail_text(path, max_bytes, missing_ok=missing_ok)
    rows: list[dict[str, object]] = []
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows, whole


def _reason_of(row: dict[str, Any]) -> str:
    """The decision log row's own reason, else its detail, cut to the carry's bound; empty when it names neither."""
    for key in ("reason", "detail"):
        value = row.get(key)
        if isinstance(value, str) and value:
            return value[:REASON_MAX_LENGTH]
    return ""


class LandingCarryStateDirSource:
    """The old landing controller's last finished tick: its facts file, tick log and decision log."""

    name = "controller-state"

    def __init__(
        self,
        facts_path: Path,
        ticks_path: Path,
        decisions_path: Path,
        *,
        sidecar_path: Path,
        max_age_seconds: int,
        tail_bytes: int,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._tail_bytes = tail_bytes
        self._facts = LandingFactsFileSource(facts_path)
        self._ticks = ticks_path
        self._decisions = decisions_path
        self._sidecar = sidecar_path
        self._max_age = max_age_seconds
        self._now = now

    def read_carry(self) -> ModelLandingControllerCarry:
        facts = self._facts.read_current()
        ticks, _ = _jsonl_tail_rows(self._ticks, self._tail_bytes, missing_ok=False)
        if not ticks:
            raise SnapshotSourceError(
                f"{self._ticks} holds no finished tick in its last {self._tail_bytes} bytes"
            )
        last = ticks[-1]
        if last.get("tick") != facts.tick:
            raise ControllerTickOpenError(
                f"controller facts are tick {facts.tick}, its last finished tick is {last.get('tick')}"
            )
        try:
            at = datetime.strptime(str(last.get("ts")), _STAMP).replace(tzinfo=UTC)
        except ValueError:
            raise SnapshotSourceError(
                f"{self._ticks} last row ts {last.get('ts')!r} unreadable"
            ) from None
        age = (self._now() - at).total_seconds()
        if age > self._max_age:
            raise ControllerStateStaleError(
                f"the controller's last finished tick is {int(age)}s old, past its bound of {self._max_age}s"
            )
        decision_rows, decisions_whole = _jsonl_tail_rows(
            self._decisions, self._tail_bytes, missing_ok=True
        )
        if (
            not decisions_whole
            and decision_rows
            and decision_rows[0].get("tick") == facts.tick
        ):
            # the tail starts inside the tick's own rows: rows of the tick may lie before it
            raise SnapshotSourceError(
                f"{self._decisions} last {self._tail_bytes} bytes do not reach back before tick {facts.tick}"
            )
        if not decisions_whole and not decision_rows:
            raise SnapshotSourceError(
                f"{self._decisions} last {self._tail_bytes} bytes hold no whole row"
            )
        logged = tuple(
            ModelLandingControllerLogged(
                kind=EnumLandingActionKind(str(row["kind"])),
                subject=str(row["subject"]),
                outcome=str(row["outcome"]),
                reason=_reason_of(row),
            )
            for row in decision_rows
            if row.get("tick") == facts.tick
            and row.get("kind") in _ACTION_KINDS
            and isinstance(row.get("subject"), str)
            and row["subject"]
            and isinstance(row.get("outcome"), str)
            and row["outcome"]
        )
        return ModelLandingControllerCarry(
            controller_tick=facts.tick,
            observed_at=facts.observed_at,
            facts=facts,
            logged=logged,
            judged=self._read_judged(),
            source=self.name,
        )

    def _read_judged(self) -> tuple[ModelLandingControllerJudged, ...]:
        """The sidecar's external_judged: per pull request, each ref a worker judged at a head (OMN-22921).

        A sidecar that is missing or not an object is unread, never an empty memory: an empty one
        would make every judged pull request read as free of its blocker. A row that does not parse
        (no head, an unread stamp) is skipped, as the controller reads it.
        """
        try:
            sidecar = json.loads(self._sidecar.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise SnapshotSourceError(
                f"{self._sidecar} unreadable: {type(exc).__name__}"
            ) from None
        if not isinstance(sidecar, dict):
            raise SnapshotSourceError(f"{self._sidecar} is not a JSON object")
        rows: list[ModelLandingControllerJudged] = []
        per_pr = sidecar.get("external_judged") or {}
        if not isinstance(per_pr, dict):
            raise SnapshotSourceError(
                f"{self._sidecar} external_judged is not an object"
            )
        for short, refs in sorted(per_pr.items()):
            if not isinstance(refs, dict):
                continue
            for ref, entry in sorted(refs.items()):
                if not isinstance(entry, dict):
                    continue
                try:
                    judged = ModelLandingExternalJudged(
                        ref=ref,
                        kind=str(entry.get("kind") or ""),
                        head_sha=str(entry.get("head") or ""),
                        judged_at=datetime.strptime(
                            str(entry.get("at")), _STAMP
                        ).replace(tzinfo=UTC),
                    )
                except (ValueError, ValidationError):
                    continue
                rows.append(ModelLandingControllerJudged(pr=short, judged=judged))
        return tuple(rows)


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
    LandingFactsFileSource,
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
        LandingFactsFileSource(resolve_path(overlay.facts_path)),
        WatcherStateWriteFactsSource(
            resolve_path(overlay.watcher_state_path),
            owner=overlay.owner,
            max_age_seconds=overlay.watcher_max_age_seconds,
            now=now,
        ),
        JsonlLandingTickRecorder(resolve_path(overlay.record_journal)),
        publisher or _publisher(overlay),
    )


def build_controller_source(
    overlay: ModelSnapshotOverlay,
    *,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> LandingCarryStateDirSource | None:
    """The controller's state source when the overlay names it (the cutover instance), else None (OMN-23665)."""
    if not (
        overlay.controller_facts_path
        and overlay.controller_ticks_path
        and overlay.controller_decisions_path
        and overlay.controller_sidecar_path
        and overlay.controller_max_age_seconds
        and overlay.controller_tail_bytes
    ):
        return None
    return LandingCarryStateDirSource(
        resolve_path(overlay.controller_facts_path),
        resolve_path(overlay.controller_ticks_path),
        resolve_path(overlay.controller_decisions_path),
        sidecar_path=resolve_path(overlay.controller_sidecar_path),
        max_age_seconds=overlay.controller_max_age_seconds,
        tail_bytes=overlay.controller_tail_bytes,
        now=now,
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
    "JsonlLandingTickRecorder",
    "LandingCarryStateDirSource",
    "LandingFactsFileSource",
    "SnapshotSourceError",
    "WatcherStateWriteFactsSource",
    "build_controller_source",
    "build_snapshot_ports",
    "resolve_path",
]
