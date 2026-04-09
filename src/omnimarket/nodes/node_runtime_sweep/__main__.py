# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""CLI entry point for node_runtime_sweep.

Usage:
    python -m omnimarket.nodes.node_runtime_sweep \
        --scope all-repos \
        --dry-run

Outputs JSON to stdout: RuntimeSweepResult model.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

import yaml

from omnimarket.nodes.node_runtime_sweep.handlers.handler_runtime_sweep import (
    ModelContractInput,
    NodeRuntimeSweep,
    RuntimeSweepRequest,
)

_log = logging.getLogger(__name__)


def _collect_contracts(omni_home: str, scope: str) -> list[ModelContractInput]:
    """Walk omni_home repos and collect contract.yaml definitions."""
    root = Path(omni_home)
    contracts: list[ModelContractInput] = []

    if scope == "omnidash-only":
        repos = ["omnidash"]
    else:
        repos = [
            d.name
            for d in root.iterdir()
            if d.is_dir() and not d.name.startswith(".") and (d / "src").exists()
        ]

    for repo in repos:
        repo_dir = root / repo
        if not repo_dir.is_dir():
            continue
        for contract_path in repo_dir.rglob("contract.yaml"):
            if "nodes" not in str(contract_path):
                continue
            try:
                raw = yaml.safe_load(contract_path.read_text())
                if not isinstance(raw, dict):
                    continue
                name = raw.get("name", contract_path.parent.name)
                description = raw.get("description", "")
                handler_spec = raw.get("handler", {})
                handler_module = (
                    handler_spec.get("module", "")
                    if isinstance(handler_spec, dict)
                    else ""
                )
                event_bus = raw.get("event_bus", {})
                raw_publish = (
                    event_bus.get("publish_topics", [])
                    if isinstance(event_bus, dict)
                    else []
                )
                raw_subscribe = (
                    event_bus.get("subscribe_topics", [])
                    if isinstance(event_bus, dict)
                    else []
                )
                # Only include string topics (skip structured event model entries)
                publish_topics = [t for t in (raw_publish or []) if isinstance(t, str)]
                subscribe_topics = [
                    t for t in (raw_subscribe or []) if isinstance(t, str)
                ]
                contracts.append(
                    ModelContractInput(
                        node_name=name,
                        description=description.strip() if description else "",
                        handler_module=handler_module,
                        publish_topics=publish_topics,
                        subscribe_topics=subscribe_topics,
                    )
                )
            except Exception as exc:
                _log.warning("failed to parse %s: %s", contract_path, exc)

    return contracts


def main() -> None:
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s: %(message)s")
    omni_home = os.environ.get("OMNI_HOME", "/Volumes/PRO-G40/Code/omni_home")

    parser = argparse.ArgumentParser(
        description="Runtime registration and wiring verification."
    )
    parser.add_argument(
        "--scope",
        default="all-repos",
        choices=["all-repos", "omnidash-only"],
        help="Check scope (default: all-repos)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help="Report findings without creating Linear tickets",
    )

    args = parser.parse_args()

    contracts = _collect_contracts(omni_home, args.scope)
    if not contracts:
        _log.warning("no contract.yaml files found")

    all_publish: list[str] = []
    all_subscribe: list[str] = []
    for c in contracts:
        all_publish.extend(c.publish_topics)
        all_subscribe.extend(c.subscribe_topics)

    request = RuntimeSweepRequest(
        contracts=contracts,
        topic_producers=all_publish,
        topic_consumers=all_subscribe,
        dry_run=args.dry_run,
    )

    handler = NodeRuntimeSweep()
    result = handler.handle(request)

    sys.stdout.write(result.model_dump_json(indent=2) + "\n")

    if result.findings:
        sys.exit(1)


if __name__ == "__main__":
    main()
