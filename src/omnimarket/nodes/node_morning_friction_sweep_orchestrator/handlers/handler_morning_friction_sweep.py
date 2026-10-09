# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Definition-B friction sweep: typed command in, typed terminal out.

Guards the run (same-date idempotency precheck, delegation evidence, premise
audit), fans out the five concurrent sources, then synthesizes, adjudicates
and reports. Phase work crosses the injected bus gateway; the deployment's
briefs, paths, lanes and bounds arrive through a strict overlay, and the phase
schemas are packaged with the node.
"""

from __future__ import annotations

import asyncio
import copy
import json
import os
import re
from datetime import UTC, datetime
from importlib.resources import files
from pathlib import Path
from typing import cast

import yaml
from omnibase_core.protocols.event_bus.protocol_event_bus_publisher import (
    ProtocolEventBusPublisher,
)
from pydantic import JsonValue, ValidationError

from ..models.model_friction_overlay import ModelFrictionOverlay
from ..models.model_friction_phase_results import (
    ModelFrictionAdjudication,
    ModelFrictionPrecheck,
    ModelFrictionPrecheckUnavailable,
    ModelFrictionReport,
    ModelFrictionScan,
    ModelFrictionSource,
    ModelFrictionSynthesis,
)
from ..models.model_morning_friction_sweep import (
    ModelFrictionPhaseRequest,
    ModelMorningFrictionSweepRequest,
    ModelMorningFrictionSweepResult,
)
from ..protocols.protocol_friction_phase_gateway import ProtocolFrictionPhaseGateway

PACKAGE = "omnimarket.nodes.node_morning_friction_sweep_orchestrator"
NODE_NAME = "node_morning_friction_sweep_orchestrator"
OVERLAY_ENV = "OMNIMARKET_MORNING_FRICTION_SWEEP_OVERLAY"
ROOTS_ENV = "ONEX_SKILL_OVERLAY_ROOTS"
DELEGATION_REASON_RE = re.compile(r"^(route-refused:\S+|route-unavailable:\S+)$")
SOURCE_LABELS = (
    "friction-source-linear",
    "friction-source-checkpoints",
    "friction-source-ci",
    "friction-source-guards",
)
DELEGATED_LABELS = ("friction-synthesize", "friction-adjudicate", "friction-report")
SCHEMA_NAMES = {
    "friction-precheck": "PRECHECK",
    "friction-scan": "SCAN",
    "friction-synthesize": "SYNTHESIZE",
    "friction-adjudicate": "ADJUDICATE",
    "friction-report": "REPORT",
}
ISO_SECONDS = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z")


class MorningFrictionSweepConfigurationError(ValueError):
    """Missing or invalid deployment overlay."""


def load_friction_overlay() -> ModelFrictionOverlay:
    """Read the explicit pointer, otherwise the first existing overlay in root order.

    The node cannot run without its overlay: an absent one is a hard stop, never
    a run against empty briefs. An explicit pointer never falls through.
    """
    if OVERLAY_ENV in os.environ:
        pointer = os.environ[OVERLAY_ENV]
        if not pointer:
            raise MorningFrictionSweepConfigurationError(
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
        raise MorningFrictionSweepConfigurationError(
            f"no friction sweep overlay: set {OVERLAY_ENV} to an overlay.yaml, or "
            f"put {NODE_NAME}/overlay.yaml under a root in {ROOTS_ENV}"
        )
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError("overlay must be a mapping")
        briefs = path.parent / str(raw.get("briefs", ""))
        if not raw.get("briefs") or not briefs.is_file():
            raise ValueError("overlay briefs file is not readable")
        return ModelFrictionOverlay.from_documents(
            {k: v for k, v in raw.items() if k != "briefs"},
            briefs.read_text(encoding="utf-8"),
        )
    except (OSError, UnicodeError, yaml.YAMLError, ValueError, TypeError) as exc:
        raise MorningFrictionSweepConfigurationError(
            f"{source}: invalid friction sweep overlay: {exc}"
        ) from None


def _integer(value: JsonValue) -> bool:
    return type(value) is int and value >= 0


def delegation_problem(cell: JsonValue) -> str | None:
    """Delegation receipt rule: zero delegation needs a typed route reason."""
    if not isinstance(cell, dict):
        return "missing or invalid cell"
    count = cell.get("delegated")
    if not _integer(count):
        return "invalid delegated count"
    runs = cell.get("runs")
    if not isinstance(runs, list) or not all(
        isinstance(r, str) and r and not re.search(r"\s", r) for r in runs
    ):
        return "invalid run ids"
    reason = cell.get("reason")
    if not isinstance(reason, str):
        return "missing or invalid reason"
    if cast(int, count) > 0:
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


def premise_audit_problem(cell: JsonValue, control_lanes: list[str]) -> str | None:
    """Measurement guard: complete coverage and every cited positive control."""
    if not isinstance(cell, dict):
        return "missing or invalid audit"
    landed = cell.get("rule_landed_at")
    evidence = cell.get("rule_evidence")
    reason = cell.get("reason")
    if not all(isinstance(v, str) for v in (landed, evidence, reason)):
        return "invalid audit evidence"
    eligible = cell.get("eligible_terminal_rows")
    controls = cell.get("positive_controls")
    unreadable = cell.get("unreadable_sources")
    if (
        not _integer(eligible)
        or not isinstance(controls, list)
        or not isinstance(unreadable, list)
        or not all(isinstance(s, str) and s.strip() for s in unreadable)
    ):
        return "invalid audit coverage"
    for control in controls:
        if (
            not isinstance(control, dict)
            or control.get("lane") not in control_lanes
            or not _integer(control.get("terminal_rows"))
            or not isinstance(control.get("evidence"), str)
        ):
            return "invalid positive controls"
    falsified = cell.get("falsified_terminal_rows")
    if cell.get("status") == "UNKNOWN":
        if falsified is None and isinstance(reason, str) and reason.strip():
            return None
        return "UNKNOWN must explain missing evidence and cannot report a count"
    if cell.get("status") != "MEASURED":
        return "invalid audit status"
    try:
        if (
            not isinstance(landed, str)
            or not ISO_SECONDS.fullmatch(landed)
            or datetime.fromisoformat(landed)
            .astimezone(UTC)
            .strftime("%Y-%m-%dT%H:%M:%SZ")
            != landed
            or not isinstance(evidence, str)
            or not evidence.strip()
        ):
            return "rule landing lacks timestamp or citation"
    except ValueError:
        return "rule landing lacks timestamp or citation"
    if eligible == 0 or unreadable or reason != "":
        return "measurement lacks complete nonempty coverage"
    if (
        not _integer(falsified)
        or not isinstance(falsified, int)
        or not isinstance(eligible, int)
        or falsified > eligible
    ):
        return "invalid falsified count"
    if len(controls) != len(control_lanes) or any(
        sum(
            1
            for c in controls
            if isinstance(c, dict)
            and c.get("lane") == lane
            and isinstance(c.get("terminal_rows"), int)
            and cast(int, c["terminal_rows"]) > 0
            and isinstance(c.get("evidence"), str)
            and cast(str, c["evidence"]).strip()
        )
        != 1
        for lane in control_lanes
    ):
        return "all cited positive controls are required"
    return None


class HandlerMorningFrictionSweep:
    """Coordinate the existing delegation pipeline, preserving the sweep's gates."""

    def __init__(
        self,
        event_bus: ProtocolEventBusPublisher | None,
        gateway: ProtocolFrictionPhaseGateway | None = None,
        overlay: ModelFrictionOverlay | None = None,
    ) -> None:
        from .handler_friction_phase_bus import HandlerFrictionPhaseBus

        self.overlay = overlay or load_friction_overlay()
        self.gateway = gateway or HandlerFrictionPhaseBus(event_bus)
        self.schemas = cast(
            dict[str, dict[str, JsonValue]],
            json.loads(
                files(PACKAGE).joinpath("models/phase_schemas.json").read_text()
            ),
        )

    async def handle(
        self, request: ModelMorningFrictionSweepRequest
    ) -> ModelMorningFrictionSweepResult:
        slots = self._slots(request)
        violations: list[str] = []

        async def call(label: str) -> dict[str, JsonValue] | None:
            result = await self.gateway.run(
                request,
                ModelFrictionPhaseRequest(
                    label=label,
                    phase=self._phase(label),
                    model="opus" if label in DELEGATED_LABELS else "sonnet",
                    effort="medium" if label == "friction-precheck" else "high",
                    prompt=self._render(self.overlay.templates[label], slots),
                    schema_definition=self._schema(label),
                ),
            )
            if label in DELEGATED_LABELS:
                if not isinstance(result, dict):
                    raise ValueError(
                        f"delegation cell refused: {label}: "
                        "missing or invalid cell (absent phase result)"
                    )
                problem = delegation_problem(result.get("delegation"))
                if problem:
                    violations.append(f"{label}: {problem}")
            return result

        # A dead or unavailable precheck never suppresses the sweep.
        raw: dict[str, JsonValue] | None = None
        precheck_failure: dict[str, str] = {}
        if not request.force:
            try:
                raw = await call("friction-precheck")
            except Exception as exc:
                precheck_failure = {
                    "failure_type": type(exc).__name__,
                    "failure_reason": str(exc),
                }
        pre: ModelFrictionPrecheck | None = None
        if raw and raw.get("verdict"):
            try:
                pre = ModelFrictionPrecheck.model_validate(raw)
            except ValidationError as exc:
                precheck_failure = {
                    "failure_type": type(exc).__name__,
                    "failure_reason": "precheck result off its schema",
                }
        precheck_agents = 0 if request.force else 1
        if pre is not None and pre.verdict != "run":
            return ModelMorningFrictionSweepResult(
                correlation_id=request.correlation_id,
                tenant_id=request.tenant_id,
                date=request.date,
                dry_run=request.dry_run,
                force=request.force,
                short_circuited=pre.verdict,
                precheck=pre,
                precheck_agents=precheck_agents,
                expensive_agents_spawned=0,
                report=self.overlay.values["kb_prefix"] + slots["report_path"],
                state=self.overlay.values["kb_prefix"] + slots["state_path"],
            )
        raw_scan, *raw_sources = await asyncio.gather(
            call("friction-scan"), *(call(label) for label in SOURCE_LABELS)
        )
        scan = ModelFrictionScan.model_validate(raw_scan)
        sources = [ModelFrictionSource.model_validate(s) for s in raw_sources]
        slots["scan_handoff"] = scan.handoff_path
        slots["synthesis_inputs"] = " ".join(
            "'" + path.replace("'", "'\\''") + "'"
            for path in [scan.handoff_path, *(s.handoff_path for s in sources)]
        )
        slots["source_summary"] = "\n".join(
            f"  {s.source_id} (verdict {s.verdict}, freshness {s.freshness}): "
            f"{s.handoff_path}"
            for s in sources
        )
        synthesized_raw = await call("friction-synthesize")
        if synthesized_raw is None or not isinstance(
            synthesized_raw.get("handoff_path"), str
        ):
            raise ValueError("synthesis handoff is missing")
        slots["synthesis_handoff"] = cast(str, synthesized_raw["handoff_path"])
        unknown = synthesized_raw.get("sources_unknown")
        if not isinstance(unknown, list) or not all(
            isinstance(v, str) for v in unknown
        ):
            raise ValueError("synthesis sources_unknown is invalid")
        slots["sources_unknown"] = "; ".join(cast(list[str], unknown)) or "none"
        adjudicated_raw = await call("friction-adjudicate")
        if adjudicated_raw is None or not isinstance(
            adjudicated_raw.get("handoff_path"), str
        ):
            raise ValueError("adjudication handoff is missing")
        slots["adjudication_handoff"] = cast(str, adjudicated_raw["handoff_path"])
        reported_raw = await call("friction-report")
        problem = premise_audit_problem(
            reported_raw.get("premise_audit") if reported_raw else None,
            self.overlay.lists["premise_control_lanes"],
        )
        if problem:
            raise ValueError(f"premise audit refused: {problem}")
        if violations:
            raise ValueError("delegation cell refused: " + "; ".join(violations))
        synthesis = ModelFrictionSynthesis.model_validate(synthesized_raw)
        adjudication = ModelFrictionAdjudication.model_validate(adjudicated_raw)
        reported = ModelFrictionReport.model_validate(reported_raw)
        return ModelMorningFrictionSweepResult(
            correlation_id=request.correlation_id,
            tenant_id=request.tenant_id,
            date=request.date,
            dry_run=request.dry_run,
            force=request.force,
            short_circuited=None,
            precheck=pre
            or ModelFrictionPrecheckUnavailable(
                bypassed="args.force=true"
                if request.force
                else "unavailable — fell through to a full run",
                **precheck_failure,
            ),
            precheck_agents=precheck_agents,
            report=reported.report_path,
            state=reported.state_path,
            commit=reported.commit_sha,
            premise_audit=reported.premise_audit,
            window=slots["window"],
            scan=scan,
            sources=sources,
            sources_unknown=synthesis.sources_unknown,
            synthesis=synthesis,
            root_causes=synthesis.root_causes,
            pickups=synthesis.pickups,
            adjudication=adjudication,
            agents=precheck_agents + 8,
        )

    @staticmethod
    def _phase(label: str) -> str:
        if label == "friction-precheck":
            return "Precheck"
        if label == "friction-scan" or label.startswith("friction-source-"):
            return "Scan"
        return label.removeprefix("friction-").capitalize()

    def _schema(self, label: str) -> dict[str, JsonValue]:
        name = "SOURCE" if label.startswith("friction-source-") else SCHEMA_NAMES[label]
        schema = copy.deepcopy(self.schemas[name])
        if name == "REPORT":
            lane = cast(
                dict[str, JsonValue],
                cast(
                    dict[str, JsonValue],
                    cast(
                        dict[str, JsonValue],
                        cast(
                            dict[str, JsonValue],
                            cast(dict[str, JsonValue], schema["properties"])[
                                "premise_audit"
                            ],
                        )["properties"],
                    )["positive_controls"],
                )["items"],
            )
            cast(
                dict[str, JsonValue],
                cast(dict[str, JsonValue], lane["properties"])["lane"],
            )["enum"] = cast(
                list[JsonValue], list(self.overlay.lists["premise_control_lanes"])
            )
        return schema

    @staticmethod
    def _render(text: str, values: dict[str, str]) -> str:
        def replace(match: re.Match[str]) -> str:
            key = match[1]
            if key not in values:
                raise ValueError(f"unknown friction prompt field: {key}")
            return values[key]

        # Replace in one pass: substitutions can contain literal shell syntax.
        return re.sub(r"@@(\w+)@@", replace, text)

    def _slots(self, request: ModelMorningFrictionSweepRequest) -> dict[str, str]:
        day = request.date
        prior = request.window_start
        overlay = self.overlay
        slots = {
            **{k: self._render(v, {"date": day}) for k, v in overlay.values.items()},
            "run_date": day,
            "window": (
                f"[{prior}T00:00:00Z, {day}T00:00:00Z) — the previous UTC day, {prior}"
            ),
            "lookback_hours": f"{request.lookback_hours:g}",
            "dry_run": str(request.dry_run).lower(),
            "phase_stems": ", ".join(f"'{s}'" for s in overlay.lists["phase_stems"]),
            "ci_repos": ", ".join(overlay.lists["ci_repos"]),
            "adjudication_mode": overlay.texts[
                "adjudication_dry" if request.dry_run else "adjudication_live"
            ],
            "report_mode": overlay.texts[
                "report_dry" if request.dry_run else "report_live"
            ],
            "fences": (
                "FENCES (standing, from args.fences — do not touch these):\n- "
                + "\n- ".join(request.fences)
                + "\n"
                if request.fences
                else "FENCES: none passed for this run. Before filing or commenting on "
                "any ticket, still check for an active lane already owning that "
                "friction (open PR, in-flight ledger CLAIM without TERMINAL) and "
                "leave it alone if one exists — record it as known-in-flight instead.\n"
            ),
        }
        return slots
