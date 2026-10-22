# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Run each morning phase as an agent session that holds only its deterministic tools.

RULING 2026-10-21T22:32:15Z (OMN-17427): a morning phase agent gets no shell and no
generic tool, not even an allowlisted one, only a fixed set of specific typed tools, and
every effect is done by a tool. Each phase session's one tool source is the morning tools
server (``tools/server.py``), which lists only the tools ``tools/phase_tools.json`` names
for that phase. The session is started by the delegation call effect's agent transport
(``handler_agent_session.py --tools-only``), the sanctioned model-call boundary, which
turns every built-in tool off, loads no other server, keeps permission checks on with
that server's tools as the one grant, and refuses any wider session. A phase with no tool
set is refused before any session starts, and the ledger-reconcile step is a node step
that calls its tool directly, with no agent.

tests/nodes/node_morning_ground_state_orchestrator/test_morning_phase_tools_guard.py fails
if a phase agent is given a shell or a generic tool.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import cast

from pydantic import JsonValue

from omnimarket.nodes.node_llm_delegation_call_effect.handlers import (
    handler_agent_session,
)

from ..models.model_morning_ground_state import (
    ModelMorningGroundStateRequest,
    ModelMorningPhaseRequest,
)
from ..models.model_morning_overlay import ModelMorningOverlay
from ..tools.catalog import ModelLedgerReconcileInput
from ..tools.context import ModelMorningToolContext, build_context
from ..tools.phases import (
    node_step_tool,
    phase_lane,
    phase_note,
    phase_tools,
    refuse_stranding_commit,
)
from .handler_morning_phase_bus import RESULT_TYPES

PACKAGE = "omnimarket.nodes.node_morning_ground_state_orchestrator"
SERVER_MODULE = PACKAGE + ".tools.server"
SERVER_NAME = "morning"
#: The delegation call effect's agent transport, run as its standalone entrypoint.
TRANSPORT = Path(handler_agent_session.__file__)
#: The state directory phase sessions record into, under the overlay's tools root.
STATE_PARTS = (".onex_state", "morning-workflows", "phase-tools")
PHASE_TIMEOUT_S = handler_agent_session.TOOLS_ONLY_TIMEOUT_S + 100
#: How long claude waits for the morning MCP server to start (its MCP_TIMEOUT, in ms). Its
#: 30 s default was too short on the loaded operator host: on 2026-10-22 the ground-state and
#: triage servers, started side by side, both hit CONNECT_TIMEOUT and the phases ran with no
#: tools. A value already in the environment wins.
MCP_STARTUP_TIMEOUT_MS = 180000
EXIT_REFUSED = 65
TOOLS_NOTE = (
    "\n\nTOOLS (RULING 2026-10-21T22:32:15Z): you have no shell, no file tools and no "
    "other tool than the mcp__morning__* tools listed to you. Every read and every "
    "effect this brief needs is one of those tools; where the brief names a command, "
    "a script or a shell block, call the tool that performs it, and never try to run "
    "it. Ledger rows, scratch files and report files the brief mentions are not yours "
    "to write in this run unless a tool writes them. If a step needs something no tool "
    "does, do not improvise: report that step UNKNOWN or blocked, naming the missing "
    "operation."
)


class MorningPhaseToolsError(RuntimeError):
    """A tools-only phase session refused to start or returned no usable result."""


def mcp_config_for(context_file: Path, calls_file: Path) -> dict[str, JsonValue]:
    """The one tool server of a phase session, run by this interpreter.

    It inherits the session's environment, which is the node's: the tools read the run
    host's locations from it, and the agent can neither see nor change it.
    """
    return {
        "mcpServers": {
            SERVER_NAME: {
                "type": "stdio",
                "command": sys.executable,
                "args": [
                    "-m",
                    SERVER_MODULE,
                    "--context",
                    str(context_file),
                    "--calls",
                    str(calls_file),
                ],
            }
        }
    }


def refuse_unless_morning_server(mcp_config: dict[str, JsonValue]) -> None:
    """Raise unless the session's one tool source is the morning phase tools server."""
    servers = cast(dict[str, dict[str, JsonValue]], mcp_config.get("mcpServers", {}))
    if set(servers) != {SERVER_NAME}:
        raise MorningPhaseToolsError(
            f"refused a phase session: it must have exactly one tool server {SERVER_NAME!r}"
        )
    server = servers[SERVER_NAME]
    args = cast(list[str], server.get("args", []))
    if server.get("command") != sys.executable or args[:2] != ["-m", SERVER_MODULE]:
        raise MorningPhaseToolsError(
            "refused a phase session: the tool server must be the morning phase tools server"
        )


def session_env() -> dict[str, str]:
    """The phase session's environment: the caller's, with the MCP server's startup time."""
    return {
        **os.environ,
        "MCP_TIMEOUT": os.environ.get("MCP_TIMEOUT") or str(MCP_STARTUP_TIMEOUT_MS),
    }


def transport_argv(model: str, config: Path, schema: Path) -> list[str]:
    """The agent transport's tools-only session, by its standalone entrypoint."""
    return [
        sys.executable,
        str(TRANSPORT),
        "--tools-only",
        model,
        str(config),
        str(schema),
    ]


class HandlerMorningPhaseTools:
    """A phase gateway whose agents hold only their phase's deterministic tools."""

    def __init__(
        self,
        overlay: ModelMorningOverlay | None = None,
        state_root: Path | None = None,
    ) -> None:
        self._overlay = overlay
        self._state_root = state_root

    def _overlay_or_load(self) -> ModelMorningOverlay:
        if self._overlay is None:
            from .handler_morning_ground_state import load_morning_overlay

            self._overlay = load_morning_overlay()
        return self._overlay

    def context(
        self, run: ModelMorningGroundStateRequest, label: str, model: str = ""
    ) -> ModelMorningToolContext:
        overlay = self._overlay_or_load()
        ctx = build_context(
            phase_label=label,
            day=run.date,
            publish=run.publish,
            values=overlay.values,
            texts=overlay.texts,
        )
        defaults = overlay.request_defaults
        plans = defaults.plan_docs if run.plan_docs is None else run.plan_docs
        docs = {f"plan_doc_{i}": doc for i, doc in enumerate(plans, 1)}
        docs["must_do_doc"] = run.must_do_doc or defaults.must_do_doc
        docs["off_rails_doc"] = run.off_rails_doc or defaults.off_rails_doc
        lane, ticket = phase_lane(label)
        return ctx.model_copy(
            update={
                "lane": lane,
                "ticket": ticket,
                "model": model,
                "state_dir": self._run_dir(ctx, run),
                "window_start": run.window_start,
                "closure_probe_ticket": run.closure_probe_ticket
                or defaults.closure_probe_ticket,
                "closure_probe_ceiling_s": run.closure_probe_ceiling_seconds,
                "source_docs": docs,
                "integration_repos": list(
                    run.integration_repos
                    if run.integration_repos is not None
                    else defaults.integration_repos
                ),
            }
        )

    def _run_dir(
        self, ctx: ModelMorningToolContext, run: ModelMorningGroundStateRequest
    ) -> Path:
        root = self._state_root or ctx.root.joinpath(*STATE_PARTS)
        path = root / run.date / run.correlation_id.hex
        path.mkdir(parents=True, exist_ok=True)
        return path

    async def run(
        self, run: ModelMorningGroundStateRequest, phase: ModelMorningPhaseRequest
    ) -> dict[str, JsonValue] | None:
        phase_tools(
            phase.label
        )  # refuses a phase with no tool set before anything starts
        refuse_stranding_commit(phase.label)
        ctx = self.context(run, phase.label, phase.model)
        run_dir = self._run_dir(ctx, run)
        context_file = run_dir / f"{phase.label}.context.json"
        context_file.write_text(ctx.model_dump_json(indent=2), encoding="utf-8")
        config = mcp_config_for(context_file, run_dir / f"{phase.label}.calls.jsonl")
        refuse_unless_morning_server(config)
        config_file = run_dir / f"{phase.label}.mcp.json"
        config_file.write_text(json.dumps(config, indent=2), encoding="utf-8")
        schema_file = run_dir / f"{phase.label}.schema.json"
        schema_file.write_text(json.dumps(phase.schema_definition, sort_keys=True))
        argv = transport_argv(phase.model, config_file, schema_file)
        with tempfile.TemporaryDirectory(prefix="morning-phase-") as cwd:
            try:
                # In a thread, so two phases the node gathers (ground-state and triage) run
                # side by side as the workflow intends, instead of one blocking the loop.
                done = await asyncio.to_thread(
                    subprocess.run,
                    argv,
                    input=phase.prompt + TOOLS_NOTE + phase_note(phase.label),
                    capture_output=True,
                    text=True,
                    cwd=cwd,
                    env=session_env(),
                    timeout=PHASE_TIMEOUT_S,
                    check=False,
                )
            except subprocess.TimeoutExpired as exc:
                raise MorningPhaseToolsError(
                    f"{phase.label}: the tools-only session ran past {PHASE_TIMEOUT_S} s"
                ) from exc
        if done.returncode == EXIT_REFUSED:
            raise MorningPhaseToolsError(
                f"{phase.label}: the agent transport refused the session: {done.stderr[-400:]}"
            )
        (run_dir / f"{phase.label}.session.json").write_text(
            done.stdout, encoding="utf-8"
        )
        try:
            outcome = json.loads(done.stdout)
        except ValueError as exc:
            tail = " ".join((done.stdout + done.stderr).split())[-400:]
            raise MorningPhaseToolsError(
                f"{phase.label}: the session exited {done.returncode} with no JSON result: {tail}"
            ) from exc
        result = outcome.get("structured_output") if isinstance(outcome, dict) else None
        if not isinstance(result, dict) or outcome.get("is_error"):
            raise MorningPhaseToolsError(
                f"{phase.label}: the session returned no structured result "
                f"(subtype={outcome.get('subtype')!r}, is_error={outcome.get('is_error')!r}): "
                f"{str(outcome.get('result'))[-400:]}"
            )
        if phase.label in RESULT_TYPES:
            RESULT_TYPES[phase.label].model_validate(result)
        return cast(dict[str, JsonValue], result)

    async def reconcile(self, run: ModelMorningGroundStateRequest) -> None:
        """The ledger-reconcile node step: its tool, called by the node, no agent."""
        ctx = self.context(run, "ledger-reconcile")
        done = node_step_tool("ledger-reconcile").run(
            ctx, ModelLedgerReconcileInput(apply=run.publish, live_lanes=run.live_lanes)
        )
        run_dir = self._run_dir(ctx, run)
        (run_dir / "ledger-reconcile.result.json").write_text(
            json.dumps(done, indent=2)
        )
        if done.get("exit") not in (0, 1):
            raise MorningPhaseToolsError(
                f"ledger-reconcile exited {done.get('exit')}: {str(done.get('stderr'))[-400:]}"
            )
