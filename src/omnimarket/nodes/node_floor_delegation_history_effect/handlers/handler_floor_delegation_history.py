# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The delegation history effect: read the projection, the routing overlay and the experiment reports, fold them, write the file.

A step of the factory floor publish, beside the delegation tracker. The fold is ``node_floor_delegation_history_compute``; this
node makes the readings and writes ``delegation-history.json``. A source that cannot be read is named in the document's ``sources``;
when the projection cannot be read and a previous file exists, the previous file stays and the run reports not ok.
"""

from __future__ import annotations

import os
import re
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path

import yaml

from omnimarket.events.floor_delegation_history import (
    DateSource,
    ModelHistorySource,
)
from omnimarket.events.floor_history_request import (
    ModelBackendSettings,
    ModelHistoryRequest,
    ModelImportedExperiment,
)
from omnimarket.handlers.node_overlay_reader import load_structured_overlay
from omnimarket.handlers.routing.routing_tiers_path import (
    load_delegation_routing_overlay,
)
from omnimarket.models.delegation.model_delegation_routing_overlay import (
    ModelDelegationRoutingOverlay,
)
from omnimarket.nodes.node_floor_delegation_effect.handlers.adapter_delegation_sources import (
    CommandRunner,
    run_command,
)
from omnimarket.nodes.node_floor_delegation_effect.handlers.handler_floor_delegation import (
    write_atomic,
)
from omnimarket.nodes.node_floor_delegation_history_compute.handlers.handler_floor_delegation_history import (
    HandlerFloorDelegationHistory as HistoryFold,
)
from omnimarket.nodes.node_floor_delegation_history_effect.handlers.adapter_history_sources import (
    read_projection,
    read_reports,
    routing_settings,
)
from omnimarket.nodes.node_floor_delegation_history_effect.handlers.adapter_report_tables import (
    ReportFile,
    parse_report,
)
from omnimarket.nodes.node_floor_delegation_history_effect.models.model_floor_delegation_history import (
    ModelFloorDelegationHistoryRequest,
    ModelFloorDelegationHistoryResult,
    ModelHistoryContractRuntime,
    ModelHistoryOverlay,
    ModelHistoryRuntime,
    ModelReportSource,
)

OWN_CONTRACT = Path(__file__).resolve().parents[1] / "contract.yaml"
_STAMP = "%Y-%m-%dT%H:%M:%SZ"


def load_runtime(
    contract_path: Path = OWN_CONTRACT,
    environ: Mapping[str, str] | None = None,
    overlay: ModelHistoryOverlay | None = None,
) -> ModelHistoryRuntime:
    """The contract's runtime block with the deployment overlay; a node with no overlay refuses by name."""
    raw = yaml.safe_load(contract_path.read_text(encoding="utf-8"))
    contract = ModelHistoryContractRuntime.model_validate(raw["runtime"])
    declared = load_structured_overlay(
        contract.overlay_id,
        ModelHistoryOverlay,
        environ,
        explicit=overlay,
        file_env=contract.overlay_file_env,
    )
    return ModelHistoryRuntime(**contract.model_dump(), **declared.model_dump())


class HandlerFloorDelegationHistory:
    """Runs the delegation history step."""

    def __init__(
        self,
        runtime: ModelHistoryRuntime | None = None,
        run: CommandRunner = run_command,
        clock: Callable[[], datetime] | None = None,
        environ: Mapping[str, str] | None = None,
        routing_overlay_loader: Callable[[], ModelDelegationRoutingOverlay]
        | None = None,
    ) -> None:
        self._rt = runtime or load_runtime()
        self._run = run
        self._environ = os.environ if environ is None else environ
        self._clock = clock or (lambda: datetime.now(UTC))
        self._routing = routing_overlay_loader or load_delegation_routing_overlay

    def _experiments(
        self,
        rt: ModelHistoryRuntime,
        source: ModelReportSource,
        files: tuple[ReportFile, ...],
    ) -> tuple[list[ModelImportedExperiment], list[ModelHistorySource]]:
        found: list[ModelImportedExperiment] = []
        notes: list[ModelHistorySource] = []
        for file in files:
            base = file.name.rsplit("/", 1)[-1]
            stamp = re.search(rt.stamp_pattern, base)
            date_source: DateSource
            if stamp is not None:
                year, month, day, hour, minute = stamp.groups()
                date = f"{year}-{month}-{day}T{hour}:{minute}:00Z"
                date_source = "filename"
                report_id = re.sub(r"\.md$", "", base[: stamp.start()]).rstrip("-")
            else:
                date = datetime.fromtimestamp(file.mtime_epoch, UTC).strftime(_STAMP)
                date_source = "file_mtime"
                report_id = file.name.split("/", 1)[0]
            parsed, problems = parse_report(
                file.text,
                report_id=report_id,
                source=f"{source.id}:{file.name}",
                date=date,
                date_source=date_source,
                hash_markers=rt.reports.hash_markers,
            )
            found.extend(parsed)
            if problems and not parsed:
                notes.append(
                    ModelHistorySource(
                        id=f"report:{source.id}:{file.name}",
                        ok=False,
                        detail="; ".join(problems)[:300],
                    )
                )
        return found, notes

    def handle(
        self, request: ModelFloorDelegationHistoryRequest
    ) -> ModelFloorDelegationHistoryResult:
        rt = self._rt
        home_text = request.home or self._environ.get(rt.home_env)
        if not home_text:
            return ModelFloorDelegationHistoryResult(
                ok=False, detail=f"no registry home: pass --home or set {rt.home_env}"
            )
        target = Path(home_text) / rt.output_relpath
        now = self._clock()

        projection = read_projection(rt, self._run, self._environ)
        sources: list[ModelHistorySource] = [
            ModelHistorySource(
                id="projection",
                ok=projection.ok,
                detail=projection.reason
                or (
                    f"{len(projection.attempts)} attempt groups, {len(projection.latencies)} latency groups, "
                    f"{projection.skipped_unattributed} unattributed rows skipped"
                ),
            )
        ]
        backends: tuple[ModelBackendSettings, ...] = ()
        try:
            backends = routing_settings(self._routing())
            sources.append(
                ModelHistorySource(
                    id="routing_overlay", ok=True, detail=f"{len(backends)} backends"
                )
            )
        except Exception as exc:
            sources.append(
                ModelHistorySource(
                    id="routing_overlay", ok=False, detail=type(exc).__name__
                )
            )
        experiments: list[ModelImportedExperiment] = []
        seen_ids: set[str] = set()
        for source in rt.reports.sources:
            files, reading = read_reports(rt, self._run, source)
            sources.append(reading)
            parsed, notes = self._experiments(rt, source, files)
            sources.extend(notes)
            for exp in parsed:
                if exp.id not in seen_ids:
                    seen_ids.add(exp.id)
                    experiments.append(exp)

        document = HistoryFold().handle(
            ModelHistoryRequest(
                now=now.astimezone(UTC).strftime(_STAMP),
                policy=rt.policy,
                backends=backends,
                attempts=projection.attempts,
                latencies=projection.latencies,
                seen=projection.seen,
                experiments=tuple(experiments),
                sources=tuple(sources),
            )
        )
        if not projection.ok and target.exists():
            return ModelFloorDelegationHistoryResult(
                ok=False,
                detail=f"projection unread, previous file kept: {projection.reason}",
            )
        write_atomic(target, document.model_dump_json(indent=1).encode())
        return ModelFloorDelegationHistoryResult(
            ok=projection.ok,
            wrote=str(target),
            detail=(
                f"{document.counts.models} models, {document.counts.series} series, "
                f"{document.counts.experiments} experiments"
                if projection.ok
                else f"projection unread, no previous file: {projection.reason}"
            ),
            models=document.counts.models,
            series=document.counts.series,
            experiments=document.counts.experiments,
        )
