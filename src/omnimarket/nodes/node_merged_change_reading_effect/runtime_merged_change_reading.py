# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""``python -m omnimarket.nodes.node_merged_change_reading_effect --change <repo>#<number> ...``: print the readings as JSON.

A thin entry: it binds the real sources the contract's ``runtime`` block declares into a container and calls the
handler. Nothing is written.
"""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from omnimarket.handlers.node_overlay_reader import (
    NodeOverlayError,
    load_structured_overlay,
)
from omnimarket.models.model_merged_change_reading import ModelMcrChange, ModelMcrConfig
from omnimarket.models.model_runtime_host_reads import ModelRuntimeHostReadOverlay
from omnimarket.nodes.node_merged_change_reading_effect.handlers.handler_merged_change_reading_effect import (
    HandlerMergedChangeReadingEffect,
)
from omnimarket.nodes.node_merged_change_reading_effect.models.model_merged_change_reading_run import (
    ModelMcrLogLabels,
    ModelMcrRunnerOverlay,
    ModelMcrRunRequest,
)
from omnimarket.nodes.node_merged_change_reading_effect.sources.source_merged_change_reading import (
    SourceMergedChangeReading,
)
from omnimarket.nodes.node_merged_change_reading_effect.sources.source_runtime_log import (
    SourceRuntimeFacts,
)
from omnimarket.nodes.node_merged_running_count_effect.sources.source_deployed_provenance import (
    ModelProvenanceLabels,
    SourceDeployedProvenance,
)
from omnimarket.protocols.protocol_merged_change_sources import (
    ProtocolMergedChangeSources,
)

OWN_CONTRACT = Path(__file__).resolve().parent / "contract.yaml"
COMPUTE_CONTRACT = (
    Path(__file__).resolve().parents[1]
    / "node_merged_change_reading_compute"
    / "contract.yaml"
)


class _LocalContainer:
    """The one service this entry binds: the real sources, keyed by their protocol."""

    def __init__(self, sources: SourceMergedChangeReading) -> None:
        self._sources = sources

    def get_service(self, key: Any) -> Any:
        if key is not ProtocolMergedChangeSources:
            raise LookupError(f"no service bound for {key!r}")
        return self._sources


def load_contract_config() -> ModelMcrConfig:
    """The compute node's own declared rule config."""
    document = yaml.safe_load(COMPUTE_CONTRACT.read_text(encoding="utf-8"))
    return ModelMcrConfig.model_validate(document["merged_change_reading"])


def build_sources(
    environ: Mapping[str, str], now: datetime, *, home: Path | None = None
) -> SourceMergedChangeReading:
    """The real sources from the contract's ``runtime`` block and the overlays it names."""
    rt = yaml.safe_load(OWN_CONTRACT.read_text(encoding="utf-8"))["runtime"]
    registry = home or Path(_required(environ, rt["home_env"]))
    runners = load_structured_overlay(
        rt["runners_overlay"]["overlay_id"],
        ModelMcrRunnerOverlay,
        environ,
        file_env=rt["runners_overlay"]["overlay_file_env"],
    )
    labels = ModelMcrLogLabels.model_validate(rt["log"])
    host_reads: ModelRuntimeHostReadOverlay | None
    try:
        host_reads = load_structured_overlay(
            rt["deployed_provenance"]["overlay_id"],
            ModelRuntimeHostReadOverlay,
            environ,
            file_env=rt["deployed_provenance"]["overlay_file_env"],
        )
    except NodeOverlayError:
        host_reads = None
    provenance = None
    runtime = None
    recorded: tuple[str, ...] = ()
    if host_reads is not None:
        declared = rt["deployed_provenance"]
        provenance = SourceDeployedProvenance(
            overlay=host_reads,
            labels=ModelProvenanceLabels(
                kind=declared["kind"],
                source_template=declared["source_template"],
                git_timeout_seconds=declared["git_timeout_seconds"],
            ),
            environ=environ,
            home=registry,
            now=now,
        )
        recorded = tuple(entry.repo for entry in host_reads.repo_refs)
        runtime = SourceRuntimeFacts(
            overlay=host_reads,
            labels=labels,
            unit_hosts=runners.unit_hosts,
            environ=environ,
            manifest_url=runners.manifest_url,
        )
    return SourceMergedChangeReading(
        runners=runners,
        provenance=provenance,
        provenance_repos=recorded,
        runtime=runtime,
        labels=labels,
        home=registry,
    )


def _required(environ: Mapping[str, str], name: str) -> str:
    value = environ.get(name, "").strip()
    if not value:
        raise SystemExit(f"REFUSED merged-change-reading: {name} is not set")
    return value


def parse_change(text: str) -> ModelMcrChange:
    """``repo#number`` or ``repo#number=sha``."""
    ref, _, sha = text.partition("=")
    repo, _, number = ref.rpartition("#")
    if not repo or not number.isdigit():
        raise SystemExit(f"REFUSED merged-change-reading: {text!r} is not repo#number")
    return ModelMcrChange(
        repo=repo.rsplit("/", 1)[-1], number=int(number), merge_sha=sha
    )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="merged_change_reading")
    ap.add_argument(
        "--change",
        action="append",
        required=True,
        help="repo#number, or repo#number=merge sha",
    )
    ap.add_argument("--now", help="override the clock, UTC ISO (tests and replays)")
    ap.add_argument("--overlay", help="a YAML file holding a full ModelMcrConfig")
    args = ap.parse_args(argv)
    now = (
        datetime.fromisoformat(args.now.replace("Z", "+00:00")).astimezone(UTC)
        if args.now
        else datetime.now(UTC)
    )
    config = (
        ModelMcrConfig.model_validate(yaml.safe_load(Path(args.overlay).read_text()))
        if args.overlay
        else load_contract_config()
    )
    container: Any = _LocalContainer(build_sources(os.environ, now))
    result = HandlerMergedChangeReadingEffect(container, config).handle(
        ModelMcrRunRequest(
            changes=tuple(parse_change(c) for c in args.change), now=now, config=config
        )
    )
    sys.stdout.write(result.model_dump_json(indent=2) + "\n")
    return 0
