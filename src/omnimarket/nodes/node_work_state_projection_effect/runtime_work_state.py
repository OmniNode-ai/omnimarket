# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Read the sources, run the projection, write it where the floor and the fill dispatcher read it.

The sources are the ledger (through the reading writer's ledger row port), the PR watcher state, every plan file the
contract's plan globs match under the plans root, the newest Linear snapshot, the lane receipts and the reading rows. The
floor record and the floor page's data file are never read. Every path is the contract's `runtime:` block, relative to the
registry home; the rules are the node's overlay and the scope is the goal's own declaration (goal_scope and
goal_scope_ticket_rule overlays). A source that cannot be read is named in the projection's `unread`, never drawn empty.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import yaml

from omnimarket.events.work_state_projection import (
    ModelReadingSource,
    ModelWorkStateRequest,
    ModelWorkStateResult,
    ModelWorkStateRules,
)
from omnimarket.handlers.node_overlay_reader import (
    NodeOverlayError,
    find_node_overlay,
    load_structured_overlay,
)
from omnimarket.nodes.node_reading_writer_effect.adapters.ledger_adapters import (
    FileLedgerRowSource,
)
from omnimarket.nodes.node_reading_writer_effect.protocols import (
    ModelLedgerQuery,
    ProtocolLedgerRowSource,
)
from omnimarket.nodes.node_work_state_projection_compute.handlers.handler_work_state_projection import (
    HandlerWorkStateProjection,
)
from omnimarket.nodes.node_work_state_projection_compute.handlers.work_state_source_mapping import (
    plan_tasks_from_text,
    pull_requests_from_watcher,
    receipts_from_documents,
    scope_from_declaration,
    tickets_from_linear_snapshot,
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


def build_request(
    now: datetime,
    home: Path,
    env: Mapping[str, str],
    ledger: ProtocolLedgerRowSource | None,
    rules: ModelWorkStateRules,
) -> ModelWorkStateRequest:
    """One request from the sources the contract names. Each unread source is one `unread` entry with its cause."""
    rt = _runtime()
    unread: list[str] = []

    goal_cfg, rule_cfg = rt["goal_scope_overlay"], rt["goal_ticket_rule_overlay"]
    declaration = _read_yaml_overlay(goal_cfg["overlay_id"], goal_cfg["file_env"], env)
    if declaration is None:
        unread.append(f"scope:{goal_cfg['overlay_id']} overlay absent")
    ticket_rule = _read_yaml_overlay(rule_cfg["overlay_id"], rule_cfg["file_env"], env)
    scope = scope_from_declaration(declaration or {}, ticket_rule)

    rows: list[Any] = []
    if ledger is None:
        unread.append(f"ledger:{rt['ledger']['path_env']} unset")
    else:
        since = now - timedelta(hours=float(rt["ledger"]["lookback_hours"]))
        for row_type in rt["ledger"]["row_types"]:
            read = ledger.read(
                ModelLedgerQuery(
                    since=since, until=now + timedelta(days=1), row_type=row_type
                )
            )
            if read.error is not None:
                unread.append(f"ledger:{row_type} {read.error}")
                rows = []
                break
            rows.extend(read.rows)

    pull_requests: list[Any] = []
    watcher = (
        _env_path(env, rt["watcher"]["path_env"]) or home / rt["watcher"]["relpath"]
    )
    try:
        pull_requests = pull_requests_from_watcher(
            json.loads(watcher.read_text(encoding="utf-8")), rules
        )
    except (OSError, ValueError) as exc:
        unread.append(f"pr-watcher:{type(exc).__name__}")

    plan_tasks: list[Any] = []
    plans_root = (
        _env_path(env, rt["plans"]["root_env"])
        or (home / rt["plans"]["root_fallback_relpath"]).resolve()
    )
    if not plans_root.is_dir():
        unread.append("plans:root absent")
    else:
        for pattern in rt["plans"]["globs"]:
            for path in sorted(plans_root.glob(pattern)):
                try:
                    text = path.read_text(encoding="utf-8")
                except (OSError, UnicodeError) as exc:
                    unread.append(
                        f"plan:{path.relative_to(plans_root)} {type(exc).__name__}"
                    )
                    continue
                plan_tasks.extend(
                    plan_tasks_from_text(str(path.relative_to(plans_root)), text, rules)
                )

    tickets: list[Any] = []
    linear_dir = (
        _env_path(env, rt["linear"]["dir_env"]) or home / rt["linear"]["dir_relpath"]
    )
    snapshots = (
        sorted(linear_dir.glob(rt["linear"]["glob"])) if linear_dir.is_dir() else []
    )
    if not snapshots:
        unread.append("linear:no snapshot")
    else:
        try:
            tickets = tickets_from_linear_snapshot(
                json.loads(snapshots[-1].read_text(encoding="utf-8")), rules
            )
        except (OSError, ValueError, KeyError) as exc:
            unread.append(f"linear:{type(exc).__name__}")

    receipt_docs: list[dict[str, Any]] = []
    receipt_dir = (
        _env_path(env, rt["receipts"]["dir_env"])
        or Path.home() / rt["receipts"]["dir_fallback"]
    )
    if not receipt_dir.is_dir():
        unread.append("receipts:dir absent")
    else:
        for path in sorted(receipt_dir.glob(rt["receipts"]["glob"])):
            try:
                doc = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if isinstance(doc, dict):
                receipt_docs.append(doc)

    readings: list[ModelReadingSource] = []
    readings_path = home / rt["readings"]["relpath"]
    if readings_path.is_file():
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
    else:
        unread.append("readings:file absent")

    return ModelWorkStateRequest(
        now=now,
        rules=rules,
        scope=scope,
        tickets=tuple(tickets),
        plan_tasks=tuple(plan_tasks),
        pull_requests=tuple(pull_requests),
        ledger_rows=tuple(rows),
        receipts=tuple(receipts_from_documents(receipt_docs)),
        readings=tuple(readings),
        unread=tuple(unread),
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
    result = HandlerWorkStateProjection().handle(
        build_request(now, home, env, ledger, rules)
    )
    target = out or home / rt["output"]["relpath"]
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(".tmp")
    tmp.write_text(result.model_dump_json(indent=1), encoding="utf-8")
    tmp.replace(target)
    return result


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
    ready = result.ready()
    lines = [
        "work-state: "
        + " ".join(f"{k}={v}" for k, v in sorted(result.counts.items()))
        + f" ready_in_scope={len(ready)} unread={len(result.unread)}",
        *(f"UNKNOWN {entry}" for entry in result.unread),
    ]
    sys.stdout.write("\n".join(lines) + "\n")
    return 0


__all__ = ["build_request", "main", "run"]
