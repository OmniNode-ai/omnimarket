# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Definition-B morning orchestration: typed command in, typed terminal out.

Phase work crosses the injected bus gateway. The deployment's briefs, report
locations and defaults arrive through a strict overlay; the phase schemas are
packaged with the node.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
from importlib.resources import files
from pathlib import Path
from typing import cast

import yaml
from omnibase_core.protocols.event_bus.protocol_event_bus_publisher import (
    ProtocolEventBusPublisher,
)
from pydantic import JsonValue

from ..models.model_morning_ground_state import (
    DROPPED_HEADLINE_KEYS,
    DROPPED_SECTIONS,
    PHASE_ORDER,
    UNCONDITIONAL_PHASES,
    ModelMorningGroundStateRequest,
    ModelMorningGroundStateResult,
    ModelMorningPhaseFailure,
    ModelMorningPhaseRequest,
)
from ..models.model_morning_overlay import ModelMorningOverlay
from ..protocols.protocol_morning_phase_gateway import ProtocolMorningPhaseGateway

PACKAGE = "omnimarket.nodes.node_morning_ground_state_orchestrator"
NODE_NAME = "node_morning_ground_state_orchestrator"
OVERLAY_ENV = "OMNIMARKET_MORNING_GROUND_STATE_OVERLAY"
ROOTS_ENV = "ONEX_SKILL_OVERLAY_ROOTS"
DELEGATION_REASON_RE = re.compile(r"^(route-refused:\S+|route-unavailable:\S+)$")
LABELS = {
    "GroundState": "ground-state",
    "Triage": "morning-triage",
    "Reconcile": "plan-reconcile",
    "Integrate": "integration-plan",
    "DroppedWork": "dropped-work",
    "Goal": "session-goal",
}


class MorningGroundStateConfigurationError(ValueError):
    """Missing or invalid deployment overlay."""


def load_morning_overlay() -> ModelMorningOverlay:
    """Read the explicit pointer, otherwise the first existing overlay in root order.

    The node cannot run without its overlay: an absent one is a hard stop, never
    a run against empty briefs. An explicit pointer never falls through.
    """
    if OVERLAY_ENV in os.environ:
        pointer = os.environ[OVERLAY_ENV]
        if not pointer:
            raise MorningGroundStateConfigurationError(
                f"{OVERLAY_ENV}: empty overlay pointer"
            )
        path: Path | None = Path(pointer)
        source = OVERLAY_ENV
    else:
        path = None
        source = ROOTS_ENV
        for root in os.environ.get(ROOTS_ENV, "").split(os.pathsep):
            if not root:
                continue
            candidate = Path(root) / NODE_NAME / "overlay.yaml"
            if candidate.exists():
                path = candidate
                break
    if path is None:
        raise MorningGroundStateConfigurationError(
            f"no morning overlay: set {OVERLAY_ENV} to an overlay.yaml, or put "
            f"{NODE_NAME}/overlay.yaml under a root in {ROOTS_ENV}"
        )
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError("overlay must be a mapping")
        briefs = path.parent / str(raw.get("briefs", ""))
        specs = path.parent / str(raw.get("phase_specs", ""))
        for label, file in (("briefs", briefs), ("phase_specs", specs)):
            if not raw.get(label) or not file.is_file():
                raise ValueError(f"overlay {label} file is not readable")
        return ModelMorningOverlay.from_documents(
            {k: v for k, v in raw.items() if k not in ("briefs", "phase_specs")},
            briefs.read_text(encoding="utf-8"),
            json.loads(specs.read_text(encoding="utf-8")),
        )
    except (OSError, UnicodeError, yaml.YAMLError, ValueError, TypeError) as exc:
        raise MorningGroundStateConfigurationError(
            f"{source}: invalid morning overlay: {exc}"
        ) from None


def json_text(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def delegation_problem(cell: JsonValue) -> str | None:
    if not isinstance(cell, dict):
        return "missing or invalid cell"
    count = cell.get("delegated")
    if type(count) is not int or count < 0:
        return "invalid delegated count"
    runs = cell.get("runs")
    if not isinstance(runs, list) or not all(
        isinstance(r, str) and r and not re.search(r"\s", r) for r in runs
    ):
        return "invalid run ids"
    reason = cell.get("reason")
    if not isinstance(reason, str):
        return "missing or invalid reason"
    if count > 0:
        if not runs:
            return "delegated steps have no runs"
        if reason:
            return "delegated steps require an empty reason"
    else:
        if runs:
            return "zero delegated count requires empty runs"
        if not DELEGATION_REASON_RE.fullmatch(reason):
            return "zero delegation reason refused"
    return None


class HandlerMorningGroundState:
    """Coordinate the existing delegation pipeline, preserving morning gates."""

    def __init__(
        self,
        event_bus: ProtocolEventBusPublisher | None,
        gateway: ProtocolMorningPhaseGateway | None = None,
        overlay: ModelMorningOverlay | None = None,
    ) -> None:
        from .handler_morning_phase_bus import HandlerMorningPhaseBus

        # OMN-17427: the overlay is a deployment fact of a RUN. Auto-wiring builds
        # this handler inside the effects runtime, and a constructor that raises
        # there takes every other node's runtime down with it, so nothing here
        # touches the deployment: an absent overlay is refused when a run starts.
        self._overlay = overlay
        self.gateway = gateway or HandlerMorningPhaseBus(event_bus, overlay=overlay)
        self.schemas = cast(
            dict[str, dict[str, JsonValue]],
            json.loads(
                files(PACKAGE).joinpath("models/phase_schemas.json").read_text()
            ),
        )

    @property
    def overlay(self) -> ModelMorningOverlay:
        """The deployment overlay, read on first use; absent or invalid is a refusal."""
        if self._overlay is None:
            self._overlay = load_morning_overlay()
        return self._overlay

    @property
    def templates(self) -> dict[str, str]:
        return self.overlay.templates

    @property
    def specs(self) -> list[dict[str, JsonValue]]:
        return self.overlay.phase_specs

    async def handle(
        self, request: ModelMorningGroundStateRequest
    ) -> ModelMorningGroundStateResult:
        # Refuse a run without its overlay before any phase, reconcile or agent runs.
        _ = self.overlay
        values = self._values(request)
        outcomes: dict[str, dict[str, JsonValue] | None] = {}
        # Reconciliation stays best-effort; its failure is terminal evidence.
        ledger_reconcile_failure = None
        try:
            await self.gateway.reconcile(request)
        except Exception as exc:
            ledger_reconcile_failure = ModelMorningPhaseFailure(
                failure_type=type(exc).__name__, failure_reason=str(exc)
            )

        async def invoke(
            label: str, phase: str, model: str = "opus", effort: str = "high"
        ) -> dict[str, JsonValue] | None:
            rendered = self._render(self.templates[label], values)
            return await self.gateway.run(
                request,
                ModelMorningPhaseRequest(
                    label=label,
                    phase=phase,
                    model=model,
                    effort=effort,
                    prompt=rendered,
                    schema_definition=self.schemas[label],
                ),
            )

        raw = None
        precheck_failure: dict[str, JsonValue] = {}
        if not request.force:
            # A dead/unavailable precheck never suppresses morning work.
            try:
                raw = await invoke(
                    "idempotency-precheck", "Precheck", "sonnet", "medium"
                )
            except Exception as exc:
                precheck_failure = {
                    "failure_type": type(exc).__name__,
                    "failure_reason": str(exc),
                }
        pre = (
            raw
            if raw and isinstance(raw.get("phases"), list) and raw["phases"]
            else None
        )

        def verdict(name: str) -> dict[str, JsonValue]:
            if not pre:
                return {
                    "phase": name,
                    "verdict": "run",
                    "evidence": "",
                    "reason": "args.force=true - pre-check bypassed"
                    if request.force
                    else "pre-check unavailable; falling through to a full run",
                }
            for row in cast(list[JsonValue], pre["phases"]):
                if isinstance(row, dict) and row.get("phase") == name:
                    return row
            return {
                "phase": name,
                "verdict": "run",
                "evidence": "",
                "reason": f"pre-check returned no row for {name}",
            }

        def runs(name: str) -> bool:
            return name in UNCONDITIONAL_PHASES or verdict(name)["verdict"] == "run"

        def skipped(name: str) -> dict[str, JsonValue]:
            row = verdict(name)
            spec = next(s for s in self.specs if s["phase"] == name)
            return {
                "skipped": row["verdict"],
                "evidence": row.get("evidence") or "",
                "reason": row.get("reason") or "",
                "artifact": self._render(str(spec["artifact"]), values),
            }

        def note(name: str, path: str) -> str:
            result = outcomes.get(name)
            if runs(name):
                return f"The {name} phase RAN in this run and returned: {json_text(result)}"
            if result and result.get("skipped") == "already-delivered":
                evidence = result.get("evidence")
                return (
                    f"The {name} phase SHORT-CIRCUITED: its artifact for {request.date} was "
                    f"ALREADY delivered before this run - committed at {evidence}, working "
                    f"tree clean, contract-conformant. That is what the pre-check proved, "
                    f"and it is why no derivation was spawned. READ {path} FROM DISK and "
                    f"use it as this run's input, exactly as you would a report an agent "
                    f"had written moments ago. Do NOT re-derive it, do NOT call it stale, "
                    f"and do NOT downgrade any verdict merely because it was not produced "
                    f"inside this process. If the file is not there when you look, that "
                    f"CONTRADICTS the pre-check: report your phase BLOCKED naming {path} "
                    f"and sha {evidence}, and write nothing."
                )
            evidence = (result or {}).get("evidence") or "see the ledger"
            return (
                f"The {name} phase SHORT-CIRCUITED as PEER-OWNED: a live ledger CLAIM "
                f"({evidence}) with no TERMINAL holds that lane right now. Its artifact "
                f"{path} may therefore be ABSENT or HALF-WRITTEN. Read it ONLY if it "
                f"exists AND git status --porcelain on it is empty. Otherwise report "
                f"your OWN phase BLOCKED, naming that peer claim, and write nothing "
                f"that would race the peer. Never fabricate the missing input."
            )

        def update_notes() -> None:
            for name, var, path in [
                ("GroundState", "g", "GROUND_STATE_PATH"),
                ("Reconcile", "reconcile", "REBASELINE_PATH"),
                ("Integrate", "integrate", "INTEGRATION_PATH"),
                ("DroppedWork", "droppedWork", "DROPPED_WORK_PATH"),
            ]:
                values[f"upstreamNote('{name}', {var}, {path})"] = note(
                    name, str(values[path])
                )
            values["upstreamNoteTriage"] = note(
                "Triage", self.overlay.texts["triage_note_path"]
            )

        async def execute(name: str) -> None:
            label = LABELS[name]
            result = (
                await invoke(
                    label,
                    name,
                    "sonnet" if name in ("Integrate", "Goal") else "opus",
                    "medium" if name in ("Integrate", "Goal") else "high",
                )
                if runs(name)
                else skipped(name)
            )
            if result is None:
                raise ValueError(
                    f"delegation cell refused: {label}: missing or invalid cell "
                    f"(absent phase result)"
                )
            if "skipped" not in result:
                problem = delegation_problem(result.get("delegation"))
                if problem:
                    raise ValueError(f"delegation cell refused: {label}: {problem}")
            outcomes[name] = result

        await invoke("decisions-register", "DecisionsRegister", "sonnet", "medium")
        # GroundState and Triage were concurrent in the original workflow.
        await asyncio.gather(execute("GroundState"), execute("Triage"))
        for name in PHASE_ORDER[2:]:
            update_notes()
            await execute(name)
        gated = [p for p in PHASE_ORDER if p not in UNCONDITIONAL_PHASES]
        spawned = [p for p in gated if runs(p)]
        return ModelMorningGroundStateResult(
            correlation_id=request.correlation_id,
            tenant_id=request.tenant_id,
            date=request.date,
            publish=request.publish,
            force=request.force,
            window=f"{request.window_start}..{request.date}",
            ledger_reconcile_failure=ledger_reconcile_failure,
            precheck=pre
            or {
                "bypassed": "args.force=true"
                if request.force
                else "unavailable - fell through to a full run",
                **precheck_failure,
            },
            expensive_agents_spawned=len(spawned),
            unconditional_agents_spawned=2,
            unconditional_phases=list(UNCONDITIONAL_PHASES),
            precheck_agents=0 if request.force else 1,
            phases_run=spawned,
            phases_short_circuited=[
                f"{p}:{skipped(p)['skipped']}" for p in gated if not runs(p)
            ],
            ground=outcomes["GroundState"],
            triage=outcomes["Triage"],
            reconcile=outcomes["Reconcile"],
            integrate=outcomes["Integrate"],
            dropped_work=outcomes["DroppedWork"],
            goal=outcomes["Goal"],
        )

    @staticmethod
    def _render(text: str, values: dict[str, str]) -> str:
        def replace(match: re.Match[str]) -> str:
            key = match[1]
            if key not in values:
                raise ValueError(f"unknown morning prompt field: {key}")
            return values[key]

        # Replace in one pass: substitutions can contain literal shell syntax.
        return re.sub(r"@@(.*?)@@", replace, text, flags=re.S)

    def _values(self, request: ModelMorningGroundStateRequest) -> dict[str, str]:
        day = request.date
        overlay = self.overlay
        defaults = overlay.request_defaults
        texts = overlay.texts
        plan_docs = (
            defaults.plan_docs if request.plan_docs is None else request.plan_docs
        )
        must_do_doc = (
            defaults.must_do_doc if request.must_do_doc is None else request.must_do_doc
        )
        off_rails_doc = (
            defaults.off_rails_doc
            if request.off_rails_doc is None
            else request.off_rails_doc
        )
        integration_repos = (
            defaults.integration_repos
            if request.integration_repos is None
            else request.integration_repos
        )
        closure_probe_ticket = (
            defaults.closure_probe_ticket
            if request.closure_probe_ticket is None
            else request.closure_probe_ticket
        )
        values = {
            "date": day,
            "integrateSince": request.window_start,
            **{
                key: self._render(text, {"date": day})
                for key, text in overlay.values.items()
            },
            "closureProbeTicket": closure_probe_ticket,
            "closureProbeCeilingSeconds": str(request.closure_probe_ceiling_seconds),
            "integrationRepos.join(', ')": ", ".join(integration_repos),
            "DROPPED_HEADLINE_KEYS.join(', ')": ", ".join(DROPPED_HEADLINE_KEYS),
        }
        for key, sequence in [
            ("DROPPED_SECTIONS", DROPPED_SECTIONS),
            ("DROPPED_HEADLINE_KEYS", DROPPED_HEADLINE_KEYS),
        ]:
            values.update({f"{key}[{i}]": v for i, v in enumerate(sequence)})
        values["fences"] = (
            "FENCES (standing, from args.fences — do not touch these):\n- "
            + "\n- ".join(request.fences)
            + "\n"
            if request.fences
            else "FENCES: none passed for this run. Before acting on any ticket, still "
            "check for an active lane already owning it (open PR, in-flight "
            "ledger CLAIM without TERMINAL) and leave it alone if one exists.\n"
        )
        values["sourceDocs"] = "\n- ".join(
            [f"PLAN (falsifiable rows): {p}" for p in plan_docs]
            + (
                [f"MUST-DO (ordered landing sequence): {must_do_doc}"]
                if must_do_doc
                else []
            )
            + (
                [f"OFF-RAILS (operator top three): {off_rails_doc}"]
                if off_rails_doc
                else []
            )
        )
        values["kbResolution"] = (
            self._render(
                texts["kb_resolution_supplied"],
                {"kb_internal_path": request.kb_internal_path},
            )
            if request.kb_internal_path
            else texts["kb_resolution_unset"]
        )
        for name in [
            "KB_ROOT",
            "KB_WORKTREE",
            "KB_COMMIT",
            "DOC_RESOLUTION",
            "COMMON",
            "DELEGATION_STEP",
        ]:
            values[name] = self._render(self.templates[name], values)
        lines = []
        for i, spec in enumerate(self.specs):
            conformance = self._render(str(spec["conformance"]), values)
            if spec["phase"] == "Goal" and not request.publish:
                conformance = (
                    conformance.split(texts["goal_publish_marker"])[0]
                    + texts["goal_dry_suffix"]
                )
            lanes = cast(list[str], spec["lanes"])
            lines.append(
                f"{i + 1}. phase '{spec['phase']}' | ledger lane names: "
                f"{' | '.join(lanes)} | artifact: "
                f"{self._render(str(spec['artifact']), values)}\n"
                f"   CONFORMANCE: {conformance}"
            )
        values["precheckPhaseLines"] = "\n".join(lines)
        values["PRECHECK_BRIEF"] = self._render(
            self.templates["PRECHECK_BRIEF"], values
        )
        values[texts["publish_key"]] = (
            texts["publish"] if request.publish else texts["dry"]
        )
        return values
