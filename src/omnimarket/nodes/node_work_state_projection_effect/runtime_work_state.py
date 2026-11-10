# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Read the sources, run the projection, write it where the floor and the fill dispatcher read it.

The sources are the ledger (through the reading writer's ledger row port), the PR watcher state, every plan file the
contract's plan globs match under the plans root, the newest Linear snapshot, the lane receipts, the reading rows and the operator asks recorded as ledger commitments. The
floor record and the floor page's data file are never read. Every path is the contract's `runtime:` block, relative to the
registry home; the rules are the node's overlay and the scope is the goal's own declaration (goal_scope,
goal_scope_ticket_rule and ticket_scope overlays). Each source is read under its own overlay budget and timeout (OMN-23014): a source over its budget serves its last
snapshot marked stale with its age, and the other sources still read. A source with no usable snapshot is named in the
projection's `unread`, never drawn empty.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import tempfile
from collections.abc import Callable, Mapping
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import yaml

from omnimarket.handlers.commitment_rows import (
    fold_commitments,
)
from omnimarket.handlers.floor_projection import publish_with_overlay
from omnimarket.handlers.node_overlay_reader import (
    NodeOverlayError,
    find_node_overlay,
    load_structured_overlay,
)
from omnimarket.models.commitment import (
    ModelCommitmentRules,
)
from omnimarket.models.floor_projection import (
    ModelFloorProjectionEnvelope,
    ModelFloorProjectionPublishOverlay,
)
from omnimarket.models.model_reading import ModelLedgerEvidenceRow
from omnimarket.models.work_state_projection import (
    EnumSourceReading,
    EnumWorkStateSource,
    ModelAncestorSource,
    ModelCommitmentSource,
    ModelLaneReceiptSource,
    ModelLinearRow,
    ModelPlanTaskSource,
    ModelPullRequestSource,
    ModelReadingSource,
    ModelSourceStatus,
    ModelWorkStateRequest,
    ModelWorkStateResult,
    ModelWorkStateRules,
)
from omnimarket.nodes.node_reading_writer_effect.adapters.ledger_adapters import (
    FileLedgerRowSource,
    read_queries,
)
from omnimarket.nodes.node_reading_writer_effect.protocols import (
    ModelLedgerQuery,
    ProtocolLedgerRowSource,
)
from omnimarket.nodes.node_work_state_projection_compute.handlers.handler_work_state_projection import (
    HandlerWorkStateProjection,
)
from omnimarket.nodes.node_work_state_projection_compute.handlers.work_state_source_budget import (
    FileSnapshotStore,
    InMemorySnapshotStore,
    ProtocolSnapshotStore,
    SourceRead,
    SourceSpec,
    SourceUnreadableError,
    read_sources_budgeted,
)
from omnimarket.nodes.node_work_state_projection_compute.handlers.work_state_source_mapping import (
    linear_rows_from_snapshot,
    plan_tasks_from_text,
    pull_requests_from_watcher,
    receipts_from_documents,
    scope_from_declaration,
)

# The projection overlay belongs to the compute node; this effect reads the sources and calls it.
NODE = "node_work_state_projection_compute"
EFFECT = "node_work_state_projection_effect"
_CONTRACT = Path(__file__).with_name("contract.yaml")


def _runtime() -> dict[str, Any]:
    data = yaml.safe_load(_CONTRACT.read_text(encoding="utf-8"))
    return dict(data["runtime"])


def _env_path(env: Mapping[str, str], name: str) -> Path | None:
    value = env.get(name, "").strip()
    return Path(value) if value else None


def _read_yaml_overlay(
    overlay_id: str, file_env: str, env: Mapping[str, str]
) -> dict[str, Any] | None:
    path = _env_path(env, file_env) or find_node_overlay(overlay_id, env)
    if path is None or not path.is_file():
        return None
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    return data if isinstance(data, dict) else None


def source_readers(
    now: datetime,
    home: Path,
    env: Mapping[str, str],
    ledger: ProtocolLedgerRowSource | None,
    rules: ModelWorkStateRules,
) -> tuple[SourceSpec, ...]:
    """One reader per source, each a closure over the contract's paths. A reader that cannot read its whole source
    raises SourceUnreadableError with the cause; a part it cannot read is a note on the rows it returns."""
    rt = _runtime()

    def read_ledger() -> SourceRead:
        if ledger is None:
            raise SourceUnreadableError(f"{rt['ledger']['path_env']} unset")
        rows: list[Any] = []
        since = now - timedelta(hours=float(rt["ledger"]["lookback_hours"]))
        row_types = tuple(rt["ledger"]["row_types"])
        queries = tuple(
            ModelLedgerQuery(
                since=since, until=now + timedelta(days=1), row_type=row_type
            )
            for row_type in row_types
        )
        # One pass over the ledger files for every row type (OMN-23081); a source without the batch port reads once each.
        reads = read_queries(ledger, queries)
        for row_type, read in zip(row_types, reads, strict=True):
            if read.error is not None:
                raise SourceUnreadableError(f"{row_type} {read.error}")
            rows.extend(read.rows)
        return SourceRead(items=tuple(rows))

    def read_watcher() -> SourceRead:
        watcher = (
            _env_path(env, rt["watcher"]["path_env"]) or home / rt["watcher"]["relpath"]
        )
        try:
            return SourceRead(
                items=tuple(
                    pull_requests_from_watcher(
                        json.loads(watcher.read_text(encoding="utf-8")), rules
                    )
                )
            )
        except (OSError, ValueError) as exc:
            raise SourceUnreadableError(type(exc).__name__) from exc

    def read_plans() -> SourceRead:
        plans_root = _env_path(env, rt["plans"]["root_env"])
        if plans_root is None:
            raise SourceUnreadableError(f"{rt['plans']['root_env']} unset")
        if not plans_root.is_dir():
            raise SourceUnreadableError("root absent")
        tasks: list[Any] = []
        notes: list[str] = []
        for pattern in rt["plans"]["globs"]:
            for path in sorted(plans_root.glob(pattern)):
                try:
                    text = path.read_text(encoding="utf-8")
                except (OSError, UnicodeError) as exc:
                    notes.append(
                        f"plan:{path.relative_to(plans_root)} {type(exc).__name__}"
                    )
                    continue
                tasks.extend(
                    plan_tasks_from_text(str(path.relative_to(plans_root)), text, rules)
                )
        return SourceRead(items=tuple(tasks), notes=tuple(notes))

    def read_linear() -> SourceRead:
        linear_dir = (
            _env_path(env, rt["linear"]["dir_env"])
            or home / rt["linear"]["dir_relpath"]
        )
        snapshots = (
            sorted(linear_dir.glob(rt["linear"]["glob"])) if linear_dir.is_dir() else []
        )
        if not snapshots:
            raise SourceUnreadableError("no snapshot")
        try:
            return SourceRead(
                items=tuple(
                    linear_rows_from_snapshot(
                        json.loads(snapshots[-1].read_text(encoding="utf-8")), rules
                    )
                )
            )
        except (OSError, ValueError, KeyError) as exc:
            raise SourceUnreadableError(type(exc).__name__) from exc

    def read_receipts() -> SourceRead:
        receipt_dir = (
            _env_path(env, rt["receipts"]["dir_env"])
            or Path.home() / rt["receipts"]["dir_fallback"]
        )
        if not receipt_dir.is_dir():
            raise SourceUnreadableError("dir absent")
        docs: list[dict[str, Any]] = []
        for path in sorted(receipt_dir.glob(rt["receipts"]["glob"])):
            try:
                doc = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if isinstance(doc, dict):
                docs.append(doc)
        return SourceRead(items=tuple(receipts_from_documents(docs)))

    def read_readings() -> SourceRead:
        readings_path = home / rt["readings"]["relpath"]
        if not readings_path.is_file():
            raise SourceUnreadableError("file absent")
        readings: list[ModelReadingSource] = []
        for line in readings_path.read_text(encoding="utf-8").splitlines():
            try:
                doc = json.loads(line)
            except ValueError:
                continue
            key, value = (
                doc.get(rt["readings"]["key_field"]),
                doc.get(rt["readings"]["value_field"]),
            )
            if key is not None and value is not None:
                readings.append(ModelReadingSource(key=str(key), value=str(value)))
        return SourceRead(items=tuple(readings))

    def read_commitments() -> SourceRead:
        """Every operator ask recorded as a commitment, read over its own window: an ask outlives the lane rows' window."""
        cfg = rt["commitments"]
        if ledger is None:
            raise SourceUnreadableError(f"{rt['ledger']['path_env']} unset")
        try:
            commitment_rules = load_structured_overlay(
                cfg["overlay_id"],
                ModelCommitmentRules,
                env,
                file_env=cfg["overlay_file_env"],
            )
        except NodeOverlayError as exc:
            raise SourceUnreadableError(f"overlay {type(exc).__name__}") from exc
        read = ledger.read(
            ModelLedgerQuery(
                since=now - timedelta(hours=float(cfg["lookback_hours"])),
                until=now + timedelta(days=1),
                row_type=commitment_rules.row_type,
                term=f"{commitment_rules.kind_cell}={commitment_rules.kind_value}",
            )
        )
        if read.error is not None:
            raise SourceUnreadableError(read.error)
        folded, malformed = fold_commitments(read.rows, commitment_rules)
        return SourceRead(
            items=tuple(
                ModelCommitmentSource(
                    commitment_id=c.commitment_id,
                    ask=c.ask,
                    owner=c.owner,
                    check=c.check,
                    status=c.status.value,
                    opened_at=c.opened_at,
                    ticket=c.ticket,
                    requires=c.requires,
                    last_checked_at=c.last_checked_at,
                    last_exit=c.last_exit,
                )
                for c in folded
            ),
            notes=tuple(f"commitments:malformed {entry}" for entry in malformed),
        )

    return (
        SourceSpec(EnumWorkStateSource.LEDGER, read_ledger, ModelLedgerEvidenceRow),
        SourceSpec(
            EnumWorkStateSource.PR_WATCHER, read_watcher, ModelPullRequestSource
        ),
        SourceSpec(EnumWorkStateSource.PLANS, read_plans, ModelPlanTaskSource),
        SourceSpec(EnumWorkStateSource.LINEAR, read_linear, ModelLinearRow),
        SourceSpec(EnumWorkStateSource.RECEIPTS, read_receipts, ModelLaneReceiptSource),
        SourceSpec(EnumWorkStateSource.READINGS, read_readings, ModelReadingSource),
        SourceSpec(
            EnumWorkStateSource.COMMITMENTS, read_commitments, ModelCommitmentSource
        ),
    )


def build_request(
    now: datetime,
    home: Path,
    env: Mapping[str, str],
    ledger: ProtocolLedgerRowSource | None,
    rules: ModelWorkStateRules,
    store: ProtocolSnapshotStore | None = None,
    reader_overrides: Mapping[EnumWorkStateSource, Callable[[], SourceRead]]
    | None = None,
) -> ModelWorkStateRequest:
    """One request from the sources the contract names, each read under its own overlay budget (OMN-23014).

    A source over its budget serves its last snapshot marked stale with its age; one with no usable snapshot is one
    `unread` entry with its cause. The other sources are read at the same time and are not held back."""
    rt = _runtime()
    unread: list[str] = []

    goal_cfg, rule_cfg = rt["goal_scope_overlay"], rt["goal_ticket_rule_overlay"]
    declaration = _read_yaml_overlay(goal_cfg["overlay_id"], goal_cfg["file_env"], env)
    if declaration is None:
        unread.append(f"scope:{goal_cfg['overlay_id']} overlay absent")
    ticket_rule = _read_yaml_overlay(rule_cfg["overlay_id"], rule_cfg["file_env"], env)
    ts_cfg = rt["ticket_scope_overlay"]
    ticket_scope = _read_yaml_overlay(ts_cfg["overlay_id"], ts_cfg["file_env"], env)
    scope = scope_from_declaration(declaration or {}, ticket_rule, ticket_scope)

    specs = tuple(
        replace(spec, read=reader_overrides[spec.source])
        if reader_overrides and spec.source in reader_overrides
        else spec
        for spec in source_readers(now, home, env, ledger, rules)
    )
    reads = read_sources_budgeted(
        specs, rules.source_budgets, store or InMemorySnapshotStore(), now
    )
    statuses: list[ModelSourceStatus] = []
    for spec in specs:
        read, status = reads[spec.source]
        statuses.append(status)
        if status.reading is EnumSourceReading.UNREAD:
            unread.append(f"{spec.source.value}:{status.cause}")
        unread.extend(read.notes)

    def rows(source: EnumWorkStateSource) -> tuple[Any, ...]:
        return reads[source][0].items

    linear_rows: tuple[ModelLinearRow, ...] = rows(EnumWorkStateSource.LINEAR)
    ancestors: tuple[ModelAncestorSource, ...] = tuple(
        r.ancestor for r in linear_rows if r.ancestor is not None
    )

    return ModelWorkStateRequest(
        now=now,
        rules=rules,
        scope=scope,
        tickets=tuple(r.ticket for r in linear_rows if r.ticket is not None),
        ancestors=ancestors,
        plan_tasks=rows(EnumWorkStateSource.PLANS),
        pull_requests=rows(EnumWorkStateSource.PR_WATCHER),
        ledger_rows=rows(EnumWorkStateSource.LEDGER),
        receipts=rows(EnumWorkStateSource.RECEIPTS),
        readings=rows(EnumWorkStateSource.READINGS),
        commitments=rows(EnumWorkStateSource.COMMITMENTS),
        unread=tuple(unread),
        source_status=tuple(statuses),
    )


def run(
    env: Mapping[str, str], now: datetime, out: Path | None = None
) -> ModelWorkStateResult:
    rt = _runtime()
    home = Path(env[rt["home_env"]])
    rules = load_structured_overlay(
        NODE, ModelWorkStateRules, env, file_env=rt["overlay_file_env"]
    )
    live = _env_path(env, rt["ledger"]["path_env"])
    ledger = (
        FileLedgerRowSource(live, live.parent / rt["ledger"]["archive_subdir"])
        if live
        else None
    )
    snapshots = (
        _env_path(env, rt["snapshots"]["dir_env"]) or home / rt["snapshots"]["relpath"]
    )
    result = HandlerWorkStateProjection().handle(
        build_request(now, home, env, ledger, rules, store=FileSnapshotStore(snapshots))
    )
    target = out or home / rt["output"]["relpath"]
    target.parent.mkdir(parents=True, exist_ok=True)
    # Each publication writes its own temp file in the target's directory and replaces the target whole, so two
    # publishers (the floor tick and the fill pass) never share a file.
    fd, tmp_name = tempfile.mkstemp(
        dir=target.parent, prefix=f".{target.name}.", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(result.model_dump_json(indent=1))
        Path(tmp_name).replace(target)
    except BaseException:
        Path(tmp_name).unlink(missing_ok=True)
        raise
    return result


def publish_projection(
    result: ModelWorkStateResult, env: Mapping[str, str], now: datetime
) -> str:
    """Publish the projection as the ``work_state`` floor projection; the answer says what happened.

    ``not configured`` when no publish overlay names a bus (the file stays the only copy), ``not published`` for the in-memory
    default, ``published`` otherwise. A projection over the contract's bound raises with its size.
    """
    rt = _runtime()["publish"]
    try:
        overlay = load_structured_overlay(
            rt["overlay_id"],
            ModelFloorProjectionPublishOverlay,
            env,
            file_env=rt["file_env"],
        )
    except NodeOverlayError:
        return "not configured"
    payload = result.model_dump(mode="json")
    size = len(json.dumps(payload))
    if size > int(rt["max_payload_bytes"]):
        raise ValueError(
            f"the projection is {size} bytes, over the {rt['max_payload_bytes']} byte bound"
        )
    sent = asyncio.run(
        publish_with_overlay(
            overlay,
            env,
            rt["topic"],
            ModelFloorProjectionEnvelope(
                projection_id=overlay.projection_id,
                as_of=now,
                producer=EFFECT,
                payload=payload,
            ),
        )
    )
    return "published" if sent else "not published"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog=f"python -m omnimarket.nodes.{EFFECT}")
    parser.add_argument(
        "--out",
        type=Path,
        default=None,
        help="write the projection here instead of the contract's path",
    )
    args = parser.parse_args(argv)
    try:
        result = run(os.environ, datetime.now(UTC), args.out)
    except (NodeOverlayError, KeyError) as exc:
        sys.stderr.write(f"work-state: refused ({type(exc).__name__}: {exc})\n")
        return 2
    try:
        published = publish_projection(result, os.environ, datetime.now(UTC))
    except Exception as exc:  # a bus that refuses is a reading: the projection ages and the floor reads it unknown
        published = f"not published ({type(exc).__name__}: {exc})"
    ready = result.ready()
    lines = [
        "work-state: "
        + " ".join(f"{k}={v}" for k, v in sorted(result.counts.items()))
        + f" ready_in_scope={len(ready)} unread={len(result.unread)} projection={published}",
        *(f"UNKNOWN {entry}" for entry in result.unread),
        *(
            f"STALE {s.source.value} age={s.age_seconds:g}s {s.cause}"
            for s in result.source_status
            if s.reading is EnumSourceReading.STALE and s.age_seconds is not None
        ),
    ]
    sys.stdout.write("\n".join(lines) + "\n")
    return 0


__all__ = ["build_request", "main", "publish_projection", "run", "source_readers"]
