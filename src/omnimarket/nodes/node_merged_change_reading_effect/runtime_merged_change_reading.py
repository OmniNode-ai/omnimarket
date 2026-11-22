# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""``python -m omnimarket.nodes.node_merged_change_reading_effect --change <repo>#<number> ...``: print the readings as JSON.

A thin entry: it binds the real sources the contract's ``runtime`` block declares into a container and calls the
handler. Nothing is written.

``read --all-runners``  read every change the runner overlay declares (what the hourly unit runs).
``schedule``            install | uninstall | status | run-now, for a host in the node overlay's schedule_hosts.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from omnimarket.handlers.node_overlay_reader import (
    NodeOverlayError,
    find_node_overlay,
    load_structured_overlay,
)
from omnimarket.models.model_merged_change_reading import ModelMcrChange, ModelMcrConfig
from omnimarket.models.model_runtime_host_reads import ModelRuntimeHostReadOverlay
from omnimarket.nodes.node_merged_change_reading_effect.handlers.handler_merged_change_reading_effect import (
    HandlerMergedChangeReadingEffect,
)
from omnimarket.nodes.node_merged_change_reading_effect.handlers.schedule import (
    MergedChangeScheduler,
    load_schedule,
)
from omnimarket.nodes.node_merged_change_reading_effect.models.model_merged_change_reading_run import (
    ModelMcrLogLabels,
    ModelMcrRunnerOverlay,
    ModelMcrRunRequest,
)
from omnimarket.nodes.node_merged_change_reading_effect.overlay import (
    NODE_NAME,
    load_overlay,
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


def load_log_labels() -> ModelMcrLogLabels:
    """The runtime read labels the effect's own contract declares (``runtime.log``)."""
    rt = yaml.safe_load(OWN_CONTRACT.read_text(encoding="utf-8"))["runtime"]
    return ModelMcrLogLabels.model_validate(rt["log"])


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


def _schedule(arguments: Sequence[str]) -> int:
    parser = argparse.ArgumentParser(prog="schedule")
    verbs = parser.add_subparsers(dest="verb", required=True)
    for verb in ("install", "uninstall", "status", "run-now"):
        sub = verbs.add_parser(verb)
        sub.add_argument("--host", required=True, help="the host's name in the overlay")
        if verb == "install":
            sub.add_argument(
                "--workspace-root",
                dest="workspace_root",
                default=os.environ.get("OMNI_HOME", ""),
            )
            sub.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(list(arguments))
    scheduler = MergedChangeScheduler(
        schedule=load_schedule(), overlay=load_overlay(), home=Path.home()
    )
    if args.verb == "install":
        if not args.workspace_root:
            raise ValueError("OMNI_HOME is not set and --workspace-root was not passed")
        found = find_node_overlay(NODE_NAME)
        if found is None:
            raise NodeOverlayError(
                "the overlay file cannot be found to pin its root in the unit"
            )
        result = scheduler.install(
            args.host,
            omni_root=Path(args.workspace_root),
            overlay_root=found.parent.parent,
            dry_run=args.dry_run,
        )
    elif args.verb == "uninstall":
        result = scheduler.uninstall(args.host)
    elif args.verb == "status":
        result = scheduler.status(args.host)
    else:
        result = scheduler.run_now(args.host)
    sys.stdout.write(result.stdout)
    sys.stderr.write(result.stderr)
    return result.exit_code


def main(argv: Sequence[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if arguments and arguments[0] == "schedule":
        return _schedule(arguments[1:])
    if arguments and arguments[0] == "read":
        arguments = arguments[1:]
    ap = argparse.ArgumentParser(prog="merged_change_reading")
    ap.add_argument(
        "--change",
        action="append",
        default=[],
        help="repo#number, or repo#number=merge sha",
    )
    ap.add_argument(
        "--all-runners",
        action="store_true",
        help="read every change the runner overlay declares",
    )
    ap.add_argument("--now", help="override the clock, UTC ISO (tests and replays)")
    ap.add_argument(
        "--resolve-only",
        action="store_true",
        help="Resolve the supplied changes to merge SHAs without reading the runtime",
    )
    ap.add_argument("--overlay", help="a YAML file holding a full ModelMcrConfig")
    args = ap.parse_args(arguments)
    if not args.change and not args.all_runners:
        ap.error("pass --change repo#number (repeatable) or --all-runners")
    now = (
        datetime.fromisoformat(args.now.replace("Z", "+00:00")).astimezone(UTC)
        if args.now
        else datetime.now(UTC)
    )
    sources = build_sources(os.environ, now)
    if args.resolve_only:
        resolved = sources.resolve_changes(tuple(parse_change(c) for c in args.change))
        sys.stdout.write(
            json.dumps([change.model_dump(mode="json") for change in resolved]) + "\n"
        )
        return 0

    config = (
        ModelMcrConfig.model_validate(yaml.safe_load(Path(args.overlay).read_text()))
        if args.overlay
        else load_contract_config()
    )
    changes = [parse_change(c) for c in args.change]
    if args.all_runners:
        declared = {(c.repo, c.number) for c in changes}
        changes.extend(
            ModelMcrChange(repo=r.repo, number=r.number)
            for r in sources.read_runners()
            if (r.repo, r.number) not in declared
        )
    container: Any = _LocalContainer(sources)
    result = HandlerMergedChangeReadingEffect(container, config).handle(
        ModelMcrRunRequest(changes=tuple(changes), now=now, config=config)
    )
    sys.stdout.write(result.model_dump_json(indent=2) + "\n")
    return 0
