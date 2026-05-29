#!/usr/bin/env python3.12
# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Dogfood market node runtime route coverage without using gap compute."""

from __future__ import annotations

import argparse
import json
import tomllib
from collections import Counter
from pathlib import Path
from typing import Any

import yaml

from omnimarket.adapters.codex.local_runtime_dispatch import _resolve_node_route

_REPO_ROOT = Path(__file__).resolve().parents[2]
_NODES_DIR = _REPO_ROOT / "src/omnimarket/nodes"
_PYPROJECT = _REPO_ROOT / "pyproject.toml"


def _load_entry_points() -> list[str]:
    data = tomllib.loads(_PYPROJECT.read_text(encoding="utf-8"))
    project = data.get("project", {})
    if not isinstance(project, dict):
        return []
    entry_points = project.get("entry-points", {})
    if not isinstance(entry_points, dict):
        return []
    nodes = entry_points.get("onex.nodes", {})
    if not isinstance(nodes, dict):
        return []
    return sorted(str(name) for name in nodes)


def _node_dirs() -> list[str]:
    return sorted(
        item.name
        for item in _NODES_DIR.iterdir()
        if item.is_dir() and item.name.startswith("node_")
    )


def _contract_for(node_name: str) -> dict[str, Any]:
    contract_path = _NODES_DIR / node_name / "contract.yaml"
    raw = yaml.safe_load(contract_path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        return {}
    return raw


def _failure_bucket(message: str, contract: dict[str, Any]) -> str:
    if "input_model" in message:
        return "missing_input_model"
    if "handler module/class" in message:
        return "missing_handler_route"
    if "command/terminal topics" in message:
        event_bus = contract.get("event_bus") or {}
        subscribe_topics = event_bus.get("subscribe_topics") or []
        if not subscribe_topics:
            return "non_command_or_missing_command_topic"
        return "missing_terminal_topic"
    return "route_error"


def _runtime_addressable(contract: dict[str, Any]) -> bool:
    runtime_dispatch = contract.get("runtime_dispatch")
    if not isinstance(runtime_dispatch, dict):
        return True
    return runtime_dispatch.get("addressable") is not False


def _topic_values(raw_topics: object) -> list[str]:
    if isinstance(raw_topics, list):
        values: list[str] = []
        for item in raw_topics:
            if isinstance(item, str):
                values.append(item)
            elif isinstance(item, dict) and isinstance(item.get("name"), str):
                values.append(item["name"])
        return values
    return []


def _native_non_addressable_check(contract: dict[str, Any]) -> tuple[bool, str]:
    node_type = str(contract.get("node_type") or "").lower()
    runtime_dispatch = contract.get("runtime_dispatch") or {}
    invocation_mode = ""
    if isinstance(runtime_dispatch, dict):
        invocation_mode = str(runtime_dispatch.get("invocation_mode") or "")
    event_bus = contract.get("event_bus") or {}
    topics = contract.get("topics") or {}
    subscribe_topics: list[str] = []
    publish_topics: list[str] = []
    if isinstance(event_bus, dict):
        subscribe_topics.extend(_topic_values(event_bus.get("subscribe_topics")))
        publish_topics.extend(_topic_values(event_bus.get("publish_topics")))
    if isinstance(topics, dict):
        subscribe_topics.extend(_topic_values(topics.get("subscribes")))
        publish_topics.extend(_topic_values(topics.get("publishes")))

    if node_type == "reducer":
        if invocation_mode not in {"event_consumer", "projection_reducer"}:
            return False, "reducer lacks event/projection invocation mode"
        if not subscribe_topics:
            return False, "reducer lacks subscribed event topics"
        return True, "native reducer event subscription"

    if node_type == "orchestrator":
        if invocation_mode != "event_consumer":
            return False, "orchestrator lacks event_consumer invocation mode"
        if not subscribe_topics or not publish_topics:
            return False, "orchestrator lacks subscribed/published event topics"
        return True, "native orchestrator event bridge"

    return False, f"{node_type or 'unknown'} nodes must be command-addressable"


def build_report() -> dict[str, Any]:
    nodes = _node_dirs()
    entries = _load_entry_points()
    entry_set = set(entries)
    node_set = set(nodes)

    routable: list[dict[str, str]] = []
    skipped: list[dict[str, str]] = []
    failures: list[dict[str, str]] = []
    buckets: Counter[str] = Counter()

    for command_name in entries:
        node_name = (
            command_name
            if command_name.startswith("node_")
            else f"node_{command_name.replace('-', '_')}"
        )
        contract = _contract_for(node_name) if node_name in node_set else {}
        if contract and not _runtime_addressable(contract):
            runtime_dispatch = contract.get("runtime_dispatch") or {}
            reason = ""
            if isinstance(runtime_dispatch, dict):
                reason = str(runtime_dispatch.get("reason") or "")
            valid, native_mode = _native_non_addressable_check(contract)
            if not valid:
                buckets["invalid_non_addressable"] += 1
                failure = {
                    "command_name": command_name,
                    "node_name": node_name,
                    "bucket": "invalid_non_addressable",
                    "error": native_mode,
                }
                failures.append(failure)
                continue
            skipped_item = {
                "command_name": command_name,
                "node_name": node_name,
                "bucket": "non_addressable",
                "reason": reason,
                "native_mode": native_mode,
            }
            skipped.append(skipped_item)
            continue
        try:
            route = _resolve_node_route(command_name)
        except Exception as exc:
            bucket = _failure_bucket(str(exc), contract)
            buckets[bucket] += 1
            failure = {
                "command_name": command_name,
                "node_name": node_name,
                "bucket": bucket,
                "error": str(exc),
            }
            failures.append(failure)
            continue
        route_item = {
            "command_name": command_name,
            "node_name": route.node_name,
            "command_topic": route.command_topic,
            "terminal_topic": route.terminal_topic,
            "input_model": route.payload_model_ref,
            "handler": route.handler_ref,
        }
        routable.append(route_item)

    return {
        "summary": {
            "node_dirs": len(nodes),
            "entry_points": len(entries),
            "missing_entry_points": sorted(node_set - entry_set),
            "dangling_entry_points": sorted(entry_set - node_set),
            "routable": len(routable),
            "skipped": len(skipped),
            "failed": len(failures),
            "failure_buckets": dict(sorted(buckets.items())),
        },
        "routable": routable,
        "skipped": skipped,
        "failures": failures,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--json", action="store_true", help="Emit JSON report")
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Exit non-zero when any route-resolution failures remain",
    )
    args = parser.parse_args()

    report = build_report()
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        summary = report["summary"]
        print(
            "market-node-runtime-dogfood: "
            f"{summary['routable']} routable / {summary['entry_points']} entry points"
        )
        if summary["skipped"]:
            print(f"  skipped_non_addressable: {summary['skipped']}")
        for bucket, count in summary["failure_buckets"].items():
            print(f"  {bucket}: {count}")

    return 1 if args.strict and report["summary"]["failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
