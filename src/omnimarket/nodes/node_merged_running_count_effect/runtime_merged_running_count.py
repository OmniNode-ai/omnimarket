# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""``python -m omnimarket.nodes.node_merged_running_count_effect``: print the merged-and-running count as JSON.

A thin entry: it binds the local-file sources the contract's ``runtime`` block declares into a container
and calls the handler. Nothing is written.
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from omnimarket.nodes.node_merged_running_count_compute.models.model_merged_running_count import (
    ModelMrcConfig,
)
from omnimarket.nodes.node_merged_running_count_effect.handlers.handler_merged_running_count_effect import (
    HandlerMergedRunningCountEffect,
)
from omnimarket.nodes.node_merged_running_count_effect.models.model_merged_running_count_run import (
    ModelMergedRunningCountRunRequest,
)
from omnimarket.nodes.node_merged_running_count_effect.sources.source_local_files import (
    ModelSourceLabels,
    SourceLocalFiles,
)
from omnimarket.protocols.protocol_merged_running_sources import (
    ProtocolMergedRunningSources,
)

OWN_CONTRACT = Path(__file__).resolve().parent / "contract.yaml"


class _LocalContainer:
    """The one service this entry binds: the local-file sources, keyed by their protocol."""

    def __init__(self, sources: SourceLocalFiles) -> None:
        self._sources = sources

    def get_service(self, key: Any) -> Any:
        if key is not ProtocolMergedRunningSources:
            raise LookupError(f"no service bound for {key!r}")
        return self._sources


def _required_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise SystemExit(f"REFUSED merged-running-count: {name} is not set")
    return value


def build_sources(now: datetime) -> SourceLocalFiles:
    rt = yaml.safe_load(OWN_CONTRACT.read_text(encoding="utf-8"))["runtime"]
    home = Path(_required_env(rt["home_env"]))
    return SourceLocalFiles(
        watcher_state=Path(
            os.environ.get(rt["watcher_state_env"]) or home / rt["watcher_relpath"]
        ),
        ledger=Path(_required_env(rt["ledger_env"])),
        labels=ModelSourceLabels.model_validate(rt["labels"]),
        hosts_table=Path(_required_env(rt["hosts_table_env"])),
        placement_read=home / rt["placement_read_relpath"],
        now=now,
    )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="merged_running_count")
    ap.add_argument("--now", help="override the clock, UTC ISO (tests and replays)")
    ap.add_argument("--overlay", help="a YAML file holding a full ModelMrcConfig")
    args = ap.parse_args(argv)
    config = (
        ModelMrcConfig.model_validate(yaml.safe_load(Path(args.overlay).read_text()))
        if args.overlay
        else None
    )
    now = (
        datetime.fromisoformat(args.now.replace("Z", "+00:00")).astimezone(UTC)
        if args.now
        else None
    )
    now = now or datetime.now(UTC)
    container: Any = _LocalContainer(build_sources(now))
    handler = HandlerMergedRunningCountEffect(container)
    result = handler.handle(ModelMergedRunningCountRunRequest(now=now, config=config))
    sys.stdout.write(result.model_dump_json(indent=2) + "\n")
    return 0
