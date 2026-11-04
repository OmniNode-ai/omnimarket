# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A morning phase agent holds only its deterministic tools: never a shell, never a generic tool.

RULING 2026-10-21T22:32:15Z (OMN-17427). These tests fail if a phase session gets a
second or foreign tool source, if it is started any way but through the agent transport's
tools-only session (whose own guard is tested in test_agent_session_boundary.py), if a
phase is given a built-in tool name, or if any phase tool's input could carry a command or
a free path.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from pydantic import JsonValue

from omnimarket.nodes.node_morning_ground_state_orchestrator.handlers import (
    handler_morning_phase_tools as hpt,
)
from omnimarket.nodes.node_morning_ground_state_orchestrator.models import (
    ModelMorningGroundStateRequest,
    ModelMorningOverlay,
    ModelMorningPhaseRequest,
)
from omnimarket.nodes.node_morning_ground_state_orchestrator.tools import catalog
from omnimarket.nodes.node_morning_ground_state_orchestrator.tools.context import (
    ModelMorningToolContext,
    build_context,
    expand_command,
)
from omnimarket.nodes.node_morning_ground_state_orchestrator.tools.phases import (
    MorningPhaseToolsMissingError,
    phase_tools,
    phase_tools_contract,
    phase_writes,
)
from omnimarket.nodes.node_morning_ground_state_orchestrator.tools.server import (
    HandlerMorningToolServer,
)

from .helpers import public_overlay, request

#: Claude Code's built-in tools; none may ever be a morning phase tool.
BUILTIN_TOOLS = frozenset(
    {
        "Bash",
        "BashOutput",
        "KillShell",
        "PowerShell",
        "Read",
        "Write",
        "Edit",
        "MultiEdit",
        "NotebookEdit",
        "Glob",
        "Grep",
        "LS",
        "WebFetch",
        "WebSearch",
        "Task",
        "Agent",
        "Skill",
        "SlashCommand",
        "TodoWrite",
        "Workflow",
        "Monitor",
    }
)
#: An input field with one of these names would make a tool generic.
GENERIC_FIELDS = frozenset(
    {
        "command",
        "cmd",
        "argv",
        "args",
        "shell",
        "script",
        "code",
        "exec",
        "program",
        "path",
        "paths",
        "file",
        "filename",
        "dir",
        "directory",
        "cwd",
        "url",
        "sql",
        "query",
        "expression",
        "glob",
        "pattern",
        "regex",
        "env",
        "module",
    }
)
PHASES = sorted(cast(dict[str, object], phase_tools_contract()["phases"]))
TEXTS = {
    "ledger_reconcile_command": "reconcile-ledger --from $MORNING_TEST_ROOT",
    "decisions_register_command": "decisions-register",
    "shared_doc_scrub_command": "doc-scrub --names-file ${MORNING_TEST_ROOT}/names.txt",
    "kb_clone_refresh_command": "clone-refresh kb",
    "kb_origin_pattern": "knowledge",
    "tools_root": "$MORNING_TEST_ROOT",
    "ledger_file": "$MORNING_TEST_ROOT/docs/ledger.md",
    "kb_clone": "$MORNING_TEST_KB",
    "kb_worktree": "$MORNING_TEST_ROOT/worktrees/@@WORKTREE_TICKET@@/kb",
}


def _overlay() -> ModelMorningOverlay:
    base = public_overlay()
    return base.model_copy(update={"texts": {**base.texts, **TEXTS}})


def _env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    root = tmp_path / "root"
    (root / "docs").mkdir(parents=True)
    (root / "docs" / "ledger.md").write_text("# ledger\n")
    monkeypatch.setenv("MORNING_TEST_ROOT", str(root))
    monkeypatch.setenv("MORNING_TEST_KB", str(tmp_path / "kb"))


def _context(label: str) -> ModelMorningToolContext:
    overlay = _overlay()
    return build_context(
        phase_label=label,
        day="2026-10-21",
        publish=False,
        values=overlay.values,
        texts=overlay.texts,
    )


@pytest.mark.unit
@pytest.mark.parametrize("label", PHASES)
def test_every_phase_session_is_a_tools_only_session_with_one_tool_server(
    label: str, tmp_path: Path
) -> None:
    config = hpt.mcp_config_for(tmp_path / f"{label}.ctx.json", tmp_path / "calls")
    hpt.refuse_unless_morning_server(config)
    assert list(cast(dict[str, object], config["mcpServers"])) == ["morning"]
    argv = hpt.transport_argv("sonnet", tmp_path / "c.json", tmp_path / "s.json")
    assert argv[1:] == [
        str(hpt.TRANSPORT),
        "--tools-only",
        "sonnet",
        str(tmp_path / "c.json"),
        str(tmp_path / "s.json"),
    ]
    assert hpt.TRANSPORT.name == "handler_agent_session.py"
    assert hpt.TRANSPORT.parent.parent.name == "node_llm_delegation_call_effect"
    assert not any(word in BUILTIN_TOOLS for word in argv)


@pytest.mark.unit
def test_the_guard_refuses_a_second_or_foreign_tool_server(tmp_path: Path) -> None:
    config = hpt.mcp_config_for(tmp_path / "ctx.json", tmp_path / "calls")
    servers = cast(dict[str, dict[str, JsonValue]], config["mcpServers"])
    second = {"mcpServers": {**servers, "shell": {"command": "bash", "args": []}}}
    with pytest.raises(hpt.MorningPhaseToolsError, match="exactly one tool server"):
        hpt.refuse_unless_morning_server(cast(dict[str, JsonValue], second))
    foreign = {
        "mcpServers": {"morning": {"command": "/bin/sh", "args": ["-c", "true"]}}
    }
    with pytest.raises(hpt.MorningPhaseToolsError, match="morning phase tools server"):
        hpt.refuse_unless_morning_server(cast(dict[str, JsonValue], foreign))


def _string_schemas(
    schema: dict[str, Any], defs: dict[str, Any]
) -> list[dict[str, Any]]:
    if "$ref" in schema:
        return _string_schemas(defs[schema["$ref"].rsplit("/", 1)[-1]], defs)
    found: list[dict[str, Any]] = []
    if schema.get("type") == "string":
        found.append(schema)
    for key in ("items", "additionalProperties"):
        if isinstance(schema.get(key), dict):
            found += _string_schemas(schema[key], defs)
    for key in ("anyOf", "oneOf", "allOf"):
        for part in schema.get(key, []):
            found += _string_schemas(part, defs)
    for part in schema.get("properties", {}).values():
        found += _string_schemas(part, defs)
    return found


@pytest.mark.unit
@pytest.mark.parametrize("name", sorted(catalog.TOOLS))
def test_no_tool_takes_a_command_or_a_free_path(name: str) -> None:
    schema = cast(dict[str, Any], catalog.TOOLS[name].input_schema())
    assert schema.get("additionalProperties") is False, (
        f"{name} accepts undeclared input"
    )
    fields = set(schema.get("properties", {}))
    assert not fields & GENERIC_FIELDS, (
        f"{name} has a generic input {fields & GENERIC_FIELDS}"
    )
    for string in _string_schemas(schema, schema.get("$defs", {})):
        assert "enum" in string or "pattern" in string, (
            f"{name}: unconstrained {string}"
        )


@pytest.mark.unit
@pytest.mark.parametrize("label", PHASES)
def test_phase_tool_sets_name_built_catalog_tools_only(label: str) -> None:
    names = [tool.name for tool in phase_tools(label)]
    assert names
    assert len(names) == len(set(names))
    assert not set(names) & BUILTIN_TOOLS
    assert all(name in catalog.TOOLS for name in names)
    assert "ledger_reconcile" not in names  # a node step, never an agent tool
    writes = [t for t in phase_tools(label) if t.effect == "write"]
    assert bool(writes) == bool(phase_writes(label)), (
        "a write tool needs owned artifacts"
    )


@pytest.mark.unit
def test_guard_rejects_fixture_phase_tool_set_with_shell(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    label = PHASES[0]
    tools = phase_tools(label)
    shell = replace(tools[0], name="Bash")
    monkeypatch.setitem(globals(), "phase_tools", lambda _: [*tools, shell])
    # Exercise the existing guard itself; do not duplicate its tool policy.
    with pytest.raises(AssertionError):
        test_phase_tool_sets_name_built_catalog_tools_only(label)


@pytest.mark.unit
@pytest.mark.parametrize("label", PHASES)
def test_the_server_lists_and_runs_only_the_phase_tools(
    label: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _env(monkeypatch, tmp_path)
    server = HandlerMorningToolServer(_context(label), tmp_path / "calls.jsonl")
    listed = [t["name"] for t in server.list_tools()]
    assert listed == [t.name for t in phase_tools(label)]
    foreign = next(name for name in catalog.TOOLS if name not in listed)
    for name in (foreign, "Bash"):
        reply = server.handle(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": name},
            }
        )
        assert reply is not None
        assert cast(dict[str, Any], reply["result"])["isError"] is True


@pytest.mark.unit
def test_a_write_tool_refuses_an_artifact_its_phase_does_not_own(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _env(monkeypatch, tmp_path)
    with pytest.raises(catalog.MorningToolRefusalError, match="does not own"):
        catalog._writable(_context("decisions-register"), ["goal"])


@pytest.mark.unit
def test_tool_programs_and_places_come_from_the_overlay_never_from_the_agent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _env(monkeypatch, tmp_path)
    ctx = _context("decisions-register")
    root = os.environ["MORNING_TEST_ROOT"]
    scrub = ctx.command("shared_doc_scrub_command")
    assert scrub == ["doc-scrub", "--names-file", f"{root}/names.txt"]
    ticket = _overlay().values["WORKTREE_TICKET"]
    assert ctx.kb_worktree == Path(root) / "worktrees" / ticket / "kb"
    assert expand_command('a "$MORNING_TEST_ROOT/x y"') == ["a", f"{root}/x y"]
    monkeypatch.delenv("MORNING_TEST_KB")
    with pytest.raises(RuntimeError, match="MORNING_TEST_KB is unset"):
        _context("decisions-register")
    for tool in catalog.TOOLS.values():
        assert not set(tool.input_model.model_fields) & {"command", "argv", "program"}


@pytest.mark.unit
def test_the_mcp_server_speaks_stdio(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _env(monkeypatch, tmp_path)
    context_file = tmp_path / "ctx.json"
    context_file.write_text(_context("idempotency-precheck").model_dump_json())
    messages = [
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {"protocolVersion": "x"},
        },
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {"name": "clock_utc"},
        },
    ]
    done = subprocess.run(
        [sys.executable, "-m", hpt.SERVER_MODULE, "--context", str(context_file)],
        input="".join(json.dumps(m) + "\n" for m in messages),
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    )
    replies = [json.loads(line) for line in done.stdout.splitlines()]
    assert [r["id"] for r in replies] == [1, 2, 3]
    assert [t["name"] for t in replies[1]["result"]["tools"]] == [
        "clock_utc",
        "ledger_lane_rows",
        "ledger_row_commit",
        "kb_artifact_status",
    ]
    assert "clock_utc" in replies[2]["result"]["content"][0]["text"]


def _fake_transport(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, body: str) -> Path:
    marker = tmp_path / "transport-ran"
    fake = tmp_path / "handler_agent_session.py"
    fake.write_text(
        f"import json, sys\nopen({str(marker)!r}, 'w').write(json.dumps(sys.argv[1:]))\n"
        + body
    )
    monkeypatch.setattr(hpt, "TRANSPORT", fake)
    return marker


def _phase(
    label: str, phase: str, schema: dict[str, JsonValue]
) -> ModelMorningPhaseRequest:
    return ModelMorningPhaseRequest(
        label=label,
        phase=phase,
        model="sonnet",
        effort="medium",
        prompt="brief",
        schema_definition=schema,
    )


def _gateway(tmp_path: Path) -> hpt.HandlerMorningPhaseTools:
    return hpt.HandlerMorningPhaseTools(
        overlay=_overlay(), state_root=tmp_path / "state"
    )


@pytest.mark.unit
def test_a_phase_with_no_tool_set_is_refused_before_any_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _env(monkeypatch, tmp_path)
    marker = _fake_transport(tmp_path, monkeypatch, "")
    with pytest.raises(MorningPhaseToolsMissingError, match="never a shell"):
        asyncio.run(
            _gateway(tmp_path).run(request(), _phase("not-a-phase", "Unknown", {}))
        )
    assert not marker.exists()


@pytest.mark.unit
def test_a_committing_phase_waits_for_the_phase_that_pushes_its_branch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _env(monkeypatch, tmp_path)
    marker = _fake_transport(tmp_path, monkeypatch, "")
    from omnimarket.nodes.node_morning_ground_state_orchestrator.tools import phases

    real = phases.has_tool_set
    monkeypatch.setattr(
        phases, "has_tool_set", lambda label: label != "session-goal" and real(label)
    )
    phase = _phase("decisions-register", "DecisionsRegister", {})
    with pytest.raises(MorningPhaseToolsMissingError, match="strands the next run"):
        asyncio.run(_gateway(tmp_path).run(request(), phase))
    assert not marker.exists()


@pytest.mark.unit
@pytest.mark.parametrize("custom_state_root", [False, True])
@pytest.mark.parametrize("host_models", [False, True])
def test_the_gateway_runs_the_tools_only_transport_and_reads_its_typed_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    custom_state_root: bool,
    host_models: bool,
) -> None:
    _env(monkeypatch, tmp_path)
    result = {"date": "2026-08-30", "clock_utc": "x", "phases": [], "residuals": []}
    marker = _fake_transport(
        tmp_path,
        monkeypatch,
        f"print(json.dumps({{'is_error': False, 'structured_output': {result!r}}}))\n",
    )
    phase = _phase("idempotency-precheck", "Precheck", {"type": "object"})
    run = request()
    if host_models:
        # The installed host wrapper owns distinct request/phase model classes.
        run = cast(
            ModelMorningGroundStateRequest,
            SimpleNamespace(**run.model_dump(), window_start=run.window_start),
        )
        phase = cast(ModelMorningPhaseRequest, SimpleNamespace(**phase.model_dump()))
    gateway = (
        _gateway(tmp_path)
        if custom_state_root
        else hpt.HandlerMorningPhaseTools(overlay=_overlay())
    )
    assert asyncio.run(gateway.run(run, phase)) == result
    argv = json.loads(marker.read_text())
    assert argv[:2] == ["--tools-only", "sonnet"]
    config = json.loads(Path(argv[2]).read_text())
    assert list(config["mcpServers"]) == ["morning"]
    assert json.loads(Path(argv[3]).read_text()) == {"type": "object"}
    state = (
        tmp_path / "state"
        if custom_state_root
        else tmp_path / "root" / ".onex_state" / "morning-workflows"
    )
    receipts = list((state / "runs").glob("*/*/*.tools.json"))
    assert len(receipts) == 1
    receipt = json.loads(receipts[0].read_text())
    assert receipt["phase"] == phase.label
    assert receipt["phase_tools"] == [tool.name for tool in phase_tools(phase.label)]
    assert receipt["built_in_tools_off"] is True
    assert receipt["transport"] == "--tools-only"
    assert receipt["correlation_id"] == str(run.correlation_id)
    assert receipt["date"] == run.date


@pytest.mark.unit
def test_a_transport_refusal_is_a_typed_phase_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _env(monkeypatch, tmp_path)
    _fake_transport(
        tmp_path, monkeypatch, "sys.stderr.write('refused'); sys.exit(65)\n"
    )
    with pytest.raises(hpt.MorningPhaseToolsError, match="transport refused"):
        asyncio.run(
            _gateway(tmp_path).run(
                request(), _phase("idempotency-precheck", "Precheck", {})
            )
        )
    assert len(list((tmp_path / "state" / "runs").glob("*/*/*.tools.json"))) == 1


@pytest.mark.unit
def test_the_overlay_model_accepts_every_tool_key() -> None:
    from omnimarket.nodes.node_morning_ground_state_orchestrator.models import (
        model_morning_overlay as overlay_model,
    )
    from omnimarket.nodes.node_morning_ground_state_orchestrator.tools import context

    texts = {
        *context.COMMAND_TEXTS,
        *context.LOCATION_TEXTS,
        *context.PLACE_TEXTS,
        context.KB_ORIGIN_TEXT,
    }
    assert texts <= set(overlay_model.OPTIONAL_TEXTS) | set(
        overlay_model.REQUIRED_TEXTS
    )
    values = {*context.ARTIFACT_VALUES.values(), *context.FACT_VALUES}
    assert values <= set(overlay_model.OPTIONAL_VALUES) | set(
        overlay_model.REQUIRED_VALUES
    )


# The four recorded plan-reconcile failures: the source of the recurring missing
# tool, rather than an inference that a merge means the runtime is running.
RUNTIME_PROBE_FAILURES = (
    ("2026-10-27T10:13:15Z", "No tool executes a row's free-form Done-proof probe"),
    (
        "2026-11-02T10:42:46Z",
        "No tool runs a row's own Done-proof probe (runtime_probe missing).",
    ),
    (
        "2026-11-03T10:28:50Z",
        "No runtime_probe tool, so no row's own Done-proof probe could run",
    ),
    (
        "2026-11-04T10:30:45Z",
        "No tool executes a row's own Done-proof probe, so runtime rows stop at LANDED or UNKNOWN.",
    ),
)


@pytest.mark.unit
@pytest.mark.parametrize(("stamp", "diagnosis"), RUNTIME_PROBE_FAILURES)
def test_recorded_reconcile_failures_have_an_executable_probe_tool(
    stamp: str, diagnosis: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _env(monkeypatch, tmp_path)
    server = HandlerMorningToolServer(_context("plan-reconcile"))
    assert "runtime_probe" in server.tools, (stamp, diagnosis)
    from omnimarket.nodes.node_morning_ground_state_orchestrator.tools.phases import (
        phase_note,
    )

    guide = phase_note("plan-reconcile")
    assert "no tool executes" not in guide
    assert "runtime_probe:" not in guide


@pytest.fixture
def runtime_probe_server(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from threading import Thread

    from omnimarket.nodes.node_morning_ground_state_orchestrator.tools import (
        local_reads,
    )

    _env(monkeypatch, tmp_path)
    requests: list[str] = []

    class Runtime(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            requests.append(self.path)
            self.send_response(
                503
                if self.path == "/unhealthy"
                else 302
                if self.path == "/redirect"
                else 200
            )
            if self.path == "/redirect":
                self.send_header("Location", "/version")
            self.end_headers()
            if self.path == "/empty":
                return
            if self.path == "/oversized":
                self.wfile.write(b"x" * 33_000)
                return
            self.wfile.write(
                b'{"version":"branch-revision","nodes":["probe"],"count":1,"ready":true}'
            )

        def log_message(self, *_args: Any) -> None:
            pass

    http = ThreadingHTTPServer(("127.0.0.1", 0), Runtime)
    thread = Thread(target=http.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setattr(local_reads, "_git_blob", lambda *_args: None)
    ctx = _context("plan-reconcile").model_copy(
        update={"source_docs": {"must_do_doc": "docs/must-do.md"}}
    )
    source = ctx.root / "docs/must-do.md"
    declaration = ctx.root / "config/lab/ci_bus_lanes.yaml"
    declaration.parent.mkdir(parents=True)
    declaration.write_text(
        "lanes:\n  dev:\n    broker: localhost:19092\n    security_protocol: PLAINTEXT\n"
    )
    monkeypatch.setenv("ONEX_WORKSPACE_CONFIG_ROOT", str(ctx.root))

    def call(
        command: str, *, row_number: int = 3, source_hash: str | None = None
    ) -> tuple[bool, dict[str, Any]]:
        source.write_text(
            "| # | Work | Done-proof |\n| -- | -- | -- |\n"
            f"| 14 | Runtime revision | `{command}` |\n"
        )
        server = HandlerMorningToolServer(ctx, tmp_path / "calls.jsonl")
        ok, text = server.call(
            "runtime_probe",
            {
                "doc": "must_do_doc",
                "source_line": row_number,
                "source_sha256": source_hash
                or hashlib.sha256(source.read_bytes()).hexdigest(),
            },
        )
        return ok, json.loads(text) if ok else {"error": text}

    try:
        yield f"http://127.0.0.1:{http.server_port}", call, requests, source
    finally:
        http.shutdown()
        http.server_close()
        thread.join(timeout=2)


@pytest.mark.unit
def test_row_probe_runs_through_the_phase_server_with_fresh_output(
    runtime_probe_server: Any,
) -> None:
    origin, call, requests, source = runtime_probe_server
    command = f"curl -fsS {origin}/version | jq -e '.version == \"branch-revision\"'"
    ok, result = call(command)
    assert ok, result
    assert requests == ["/version"]
    assert result["executed"] is True
    assert result["assertion_passed"] is True
    assert result["source_command"] == command
    assert result["stdout"] == "true\n"
    assert result["observed_at"].endswith("Z")
    assert result["host"]
    assert result["source_sha256"] == hashlib.sha256(source.read_bytes()).hexdigest()


@pytest.mark.unit
def test_probe_refuses_a_changed_source_before_the_readback(
    runtime_probe_server: Any,
) -> None:
    origin, call, requests, _source = runtime_probe_server
    ok, result = call(f"curl -fsS {origin}/version", source_hash="a" * 64)
    assert not ok, result
    assert "source changed" in result["error"]
    assert requests == []


@pytest.mark.unit
@pytest.mark.parametrize("suffix", ["/unhealthy", "/version"])
def test_failed_row_probe_never_becomes_runtime_proof(
    runtime_probe_server: Any, suffix: str
) -> None:
    origin, call, requests, _source = runtime_probe_server
    ok, result = call(f"curl -fsS {origin}{suffix} | jq -e '.version == \"old\"'")
    assert ok, result
    assert requests == [suffix]
    assert result["exit"] != 0
    assert result["assertion_passed"] is not True


@pytest.mark.unit
@pytest.mark.parametrize(
    "command",
    [
        "curl -fsS {origin}/version; touch /tmp/should-not-run",
        "curl -X POST {origin}/version",
        "curl -fsS {origin}/version | sh",
        "curl -fsSL {origin}/version",
        "curl -fsS http://unbound-runtime.invalid/version",
        "curl -fsS {origin}/version | jq -e 'true'",
        "curl -fsS {origin}/version` and `curl -fsS {origin}/health",
        "curl -fsS {origin}/version?one=1&two=2",
        "curl -fsS {origin}/version | jq -e '.ready == {{\"nested\":true}}'",
    ],
)
def test_probe_refuses_mutations_unbound_targets_and_unrelated_assertions(
    runtime_probe_server: Any, command: str
) -> None:
    origin, call, requests, _source = runtime_probe_server
    ok, result = call(command.format(origin=origin))
    assert not ok, result
    assert "REFUSED" in result["error"]
    assert requests == []


@pytest.mark.unit
@pytest.mark.parametrize("suffix", ["/empty", "/oversized", "/redirect"])
def test_incomplete_readbacks_and_redirects_are_unverified(
    runtime_probe_server: Any, suffix: str
) -> None:
    origin, call, requests, _source = runtime_probe_server
    ok, result = call(f"curl -fsS {origin}{suffix}")
    assert ok, result
    assert requests == [suffix]
    assert result["exit"] != 0
    assert result["assertion_passed"] is None
    assert result["stderr"]
    assert result["truncated"] is (suffix == "/oversized")


@pytest.mark.unit
@pytest.mark.parametrize("field", ["missing", "count", "ready"])
def test_missing_fields_and_json_type_mismatches_are_unverified(
    runtime_probe_server: Any, field: str
) -> None:
    origin, call, requests, _source = runtime_probe_server
    expected = "1" if field == "ready" else "true"
    ok, result = call(f"curl -fsS {origin}/version | jq -e '.{field} == {expected}'")
    assert ok, result
    assert requests == ["/version"]
    assert result["exit"] != 0
    assert result["assertion_passed"] is not True


@pytest.mark.unit
def test_a_readback_without_an_assertion_does_not_infer_a_stage(
    runtime_probe_server: Any,
) -> None:
    origin, call, requests, _source = runtime_probe_server
    ok, result = call(f"curl -fsS {origin}/version | jq -r '.version'")
    assert ok, result
    assert requests == ["/version"]
    assert result["exit"] == 0
    assert result["stdout"] == "branch-revision\n"
    assert result["assertion_passed"] is None
    assert "stage" not in result
