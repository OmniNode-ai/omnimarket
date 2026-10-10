"""Coordinate discovery; all phase execution uses the canonical invocation node."""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from uuid import uuid5

import jsonschema
import yaml

from ..models.model_discovery_contract import (
    ModelDiscoveryContract,
    ModelDiscoveryPhaseSpec,
)
from ..models.model_discovery_request import ModelDiscoveryRequest
from ..models.model_discovery_result import (
    ModelDiscoveryPhaseResult,
    ModelDiscoveryResult,
    require_delegation,
)
from ..models.model_discovery_task import ModelDiscoveryTask

NODE_ROOT = Path(__file__).resolve().parents[1]
PROMPTS = NODE_ROOT / "handlers/prompts"


def load_contract() -> ModelDiscoveryContract:
    return ModelDiscoveryContract.model_validate(
        yaml.safe_load((NODE_ROOT / "contract.yaml").read_text())
    )


# The repo's end-of-file fixer gives every template one final line break. The old tool's
# fragments ended in a line break only for these three, so only they keep it when rendered.
_FRAGMENTS_ENDING_IN_LINE_BREAK = frozenset(
    {"common.txt", "delegation_step.txt", "rules.txt"}
)


def _render(name: str, values: dict[str, str]) -> str:
    text = (PROMPTS / name).read_text(encoding="utf-8").removesuffix("\n")
    if name in _FRAGMENTS_ENDING_IN_LINE_BREAK:
        text += "\n"
    # Values may contain braces themselves; insert once, never evaluate user text.
    return re.sub(r"\{\{([a-z_]+)\}\}", lambda m: values[m[1]], text)


def prompt_values(request: ModelDiscoveryRequest) -> dict[str, str]:
    values = {
        "date": request.date,
        "substrate_probe_url": request.overlay.substrate_probe_url,
        "registry_dir": request.overlay.registry_dir,
    }
    values["delegation_step"] = _render("delegation_step.txt", values)
    values["fences"] = (
        "FENCES (standing, from args.fences — do not slate, recommend, or file against these):\n- "
        + "\n- ".join(request.fences)
        + "\n"
        if request.fences
        else "FENCES: none passed for this run. Before slating any item, still check for "
        "an active lane "
        "already owning it (open PR, in-flight ledger CLAIM without TERMINAL, a plan whose "
        "execution "
        "gate is CLOSED) and exclude it — record it as already-owned instead.\n"
    )
    values["common"] = _render("common.txt", values)
    values["rules"] = _render("rules.txt", values)
    values["kb_root"] = _render("kb_root.txt", values)
    values["profiles"] = (
        json.dumps(
            [p.model_dump(mode="json") for p in request.profiles],
            ensure_ascii=False,
            separators=(",", ":"),
        )
        if request.profiles
        else "none — produce the unassigned inventory only, per RULE 6's no-profiles default"
    )
    values["slate_order"] = (
        " → ".join(p.name for p in request.profiles) + " → session-lane"
        if request.profiles
        else "session-lane only (no profiles supplied — unassigned inventory)"
    )
    values["state_instruction"] = _render(
        "state_true.txt" if request.write_state else "state_false.txt", values
    )
    values["state_commit_path"] = (
        " beta/tracking/architecture-work-discovery-state.json"
        if request.write_state
        else ""
    )
    return values


class HandlerArchitectureDiscovery:
    """Definition-B coordination, with a typed phase execution seam for lab replay."""

    def __init__(
        self,
        execute: Callable[[ModelDiscoveryTask], ModelDiscoveryPhaseResult]
        | None = None,
    ) -> None:
        if execute is None:
            from ..adapters.handler_coding_agent_bus import HandlerCodingAgentBus

            execute = HandlerCodingAgentBus().execute
        self._execute = execute

    def handle(self, request: ModelDiscoveryRequest) -> ModelDiscoveryResult:
        contract = load_contract()
        schemas = json.loads((NODE_ROOT / "handlers/schemas.json").read_text())
        phases = contract.phases
        values = prompt_values(request)

        def run(phase: ModelDiscoveryPhaseSpec) -> ModelDiscoveryPhaseResult:
            task = ModelDiscoveryTask(
                label=phase.label,
                phase=phase.phase,
                model=phase.model,
                effort=phase.effort,
                prompt=_render(phase.prompt, values),
                schema_definition=schemas[phase.phase],
                workspace_path=request.workspace_path,
                correlation_id=uuid5(request.correlation_id, phase.label),
                timeout_ms=contract.invocation.timeout_ms,
            )
            result = self._execute(task)
            payload = result.model_dump(mode="json", exclude_none=True)
            require_delegation(task.label, payload)
            jsonschema.validate(payload, task.schema_definition)
            return result

        # Await the full scan wave before reading any handoff or scheduling adjudication.
        with ThreadPoolExecutor(max_workers=4) as pool:
            futures = [pool.submit(run, phase) for phase in phases[:4]]
            scans = [future.result() for future in futures]
        for name, scan in zip(
            ("linear", "marketplace", "plans", "process"), scans, strict=True
        ):
            assert scan.handoff_path is not None
            values[name] = scan.handoff_path
        adjudication = run(phases[4])
        assert adjudication.handoff_path is not None
        values["adjudication"] = adjudication.handoff_path
        report = run(phases[5])
        assert report.report_path is not None
        assert report.state_path is not None
        assert report.commit_sha is not None
        assert report.slate_summary is not None
        return ModelDiscoveryResult(
            date=request.date,
            write_state=request.write_state,
            profiles=request.profiles,
            report=report.report_path,
            delegation=report.delegation,
            state=report.state_path,
            commit=report.commit_sha,
            slate=report.slate_summary,
            scan=dict(
                zip(("linear", "market", "plans", "process"), scans, strict=True)
            ),
            adjudication=adjudication,
        )
