"""Failing-first contract for lab-only agent dispatch (OMN-20894)."""

from __future__ import annotations

import json
import os
import re
import socket
import subprocess
import sys
from collections.abc import Mapping
from dataclasses import FrozenInstanceError
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import Mock

import pytest

from omnimarket.nodes.node_operator_access_mode_effect.handlers import (
    handler_operator_hook_cli as cli,
)
from omnimarket.nodes.node_operator_access_mode_effect.handlers.handler_lab_dispatch_gate import (
    MAC_DESTINATIONS,
    HandlerLabDispatchGate,
    LabPool,
    dispatch_text,
    read_lab_pool,
    route_destinations,
)
from omnimarket.nodes.node_operator_access_mode_effect.handlers.handler_operator_hooks_installer import (
    ENTRY_MODULE,
    REGISTRATIONS,
    install_operator_hooks,
)
from omnimarket.nodes.node_operator_access_mode_effect.models import (
    LabPool as ExportedLabPool,
)
from omnimarket.nodes.node_operator_access_mode_effect.models import (
    ModelLabDispatchRequest,
)
from omnimarket.nodes.node_operator_access_mode_effect.models.model_lab_dispatch_request import (
    LabPool as ModelLabPool,
)
from omnimarket.nodes.node_operator_access_mode_effect.models.model_lab_dispatch_request import (
    ModelLabDispatchRequest as LabDispatchRequest,
)

pytestmark = pytest.mark.unit
FIXTURES = Path(__file__).resolve().parent / "fixtures"
NOW = datetime(2026, 10, 25, 12, 0, tzinfo=UTC)
RUNNER_COMMAND = (
    'python3 "${CLAUDE_PLUGIN_ROOT}/skills/remote-lane/scripts/onex_remote_lane.py" '
    "--brief <brief file> --lane <lane> --ticket <OMN-n> --model sonnet "
    "--parent <orchestrator lane> --"
)


def _sample(name: str) -> tuple[dict[str, str], dict[str, Any]]:
    loaded = json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))
    return dict(loaded["environ"]), dict(loaded["payload"])


def _environ(name: str, state: Path, **extra: str) -> dict[str, str]:
    environ, _ = _sample(name)
    environ["OMNI_ACCESS_MODE_STATE_DIR"] = str(state)
    environ.update(extra)
    return environ


def _run(
    event: str, payload: dict[str, Any], environ: dict[str, str]
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-B", "-m", ENTRY_MODULE, event],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
        env={
            "PATH": os.environ.get("PATH", ""),
            "HOME": "/nonexistent",
            "PYTHONPATH": str(Path(__file__).resolve().parents[3] / "src"),
            **environ,
        },
    )


def _advertisement(**overrides: Any) -> dict[str, Any]:
    return {
        "host_name": "lab-worker-h201",
        "cores": 8,
        "load1": 1.0,
        "mem_available_bytes": 8 * 1024**3,
        "running_units": 1,
        "max_units": 4,
        "advertised_at": (NOW - timedelta(seconds=30))
        .isoformat()
        .replace("+00:00", "Z"),
        "cadence_seconds": 60,
        **overrides,
    }


@pytest.fixture
def lab_environ(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    monkeypatch.setattr(socket, "gethostname", lambda: "launching-mac.example.test")
    return {
        "ONEX_LAB_WORK_PYTHON": sys.executable,
        "OMNI_HOME": str(tmp_path),
    }


def _read_pool(
    advertisements: list[dict[str, Any]], environ: Mapping[str, str]
) -> LabPool:
    run = Mock(
        return_value=subprocess.CompletedProcess([], 0, json.dumps(advertisements), "")
    )
    return read_lab_pool(environ, run=run, now=NOW)


def _assert_dispatch_reason(reason: str, *, free: int, calls: int) -> None:
    assert reason.startswith("BLOCKED (lab-only agents, OMN-20894): ")
    assert re.search(rf"\b{free} free slot", reason)
    assert re.search(rf"\b{calls}\s+(?:agent\s+)?calls?\b", reason)
    assert "dest=mac" in reason
    assert RUNNER_COMMAND in reason
    assert "dest=lab" in reason


def test_models_are_exported_frozen_slotted_and_pool_lives_in_models() -> None:
    assert LabPool is ModelLabPool is ExportedLabPool
    assert ModelLabDispatchRequest is LabDispatchRequest
    assert LabPool.__module__.endswith(".models.model_lab_dispatch_request")
    pool = LabPool(free_slots=3, detail="three free slots")
    tool_input = {"prompt": "ROUTE: dest=mac"}
    request = ModelLabDispatchRequest(
        tool_name="Agent", tool_input=tool_input, pool=pool
    )
    assert request.tool_input == tool_input
    for instance in (pool, request):
        assert not hasattr(instance, "__dict__")
    with pytest.raises(FrozenInstanceError):
        pool.free_slots = 0
    with pytest.raises(FrozenInstanceError):
        request.tool_name = "Bash"
    assert frozenset({"mac", "local", "launching-host"}) == MAC_DESTINATIONS


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("ROUTE: dest=mac", ("mac",)),
        ("ROUTE: dest=Mac lane=one\nROUTE: dest=LAB", ("mac", "lab")),
        ("  ROUTE: dest=mac dest=h201\nROUTE: dest=local", ("mac", "h201", "local")),
        (
            "ROUTE: dest=Mac\\nignored dest=LAB\"quoted dest=local'quoted dest=h201`code",
            ("mac", "lab", "local", "h201"),
        ),
        ("Prose says dest=mac\nROUTE: dest=lab", ("lab",)),
        ("No route here: dest=mac", ()),
        ("ROUTE: lane=one\nordinary dest=mac", ()),
        ("", ()),
    ],
)
def test_route_destinations_are_ordered_lowercase_and_stop_at_delimiters(
    text: str, expected: tuple[str, ...]
) -> None:
    assert route_destinations(text) == expected


def test_agent_dispatch_text_includes_prompt_description_and_label_in_order() -> None:
    assert (
        dispatch_text(
            "Agent",
            {
                "prompt": "prompt",
                "description": "description",
                "label": "label",
                "other": "ignored",
            },
        )
        == "prompt\ndescription\nlabel"
    )
    assert dispatch_text("Agent", {}) == ""


def test_workflow_dispatch_text_prefers_script_over_script_path(tmp_path: Path) -> None:
    path = tmp_path / "workflow.py"
    path.write_text("ROUTE: dest=mac", encoding="utf-8")
    assert (
        dispatch_text(
            "Workflow", {"script": "ROUTE: dest=lab", "scriptPath": str(path)}
        )
        == "ROUTE: dest=lab"
    )
    assert dispatch_text("Workflow", {"scriptPath": str(path)}) == "ROUTE: dest=mac"


def test_workflow_dispatch_text_ignores_missing_and_non_regular_files(
    tmp_path: Path,
) -> None:
    assert dispatch_text("Workflow", {"scriptPath": str(tmp_path / "missing.py")}) == ""
    assert dispatch_text("Workflow", {"scriptPath": str(tmp_path)}) == ""
    assert dispatch_text("Workflow", {}) == ""


@pytest.mark.parametrize("destination", ["mac", "Mac", "local", "launching-host"])
def test_agent_mac_destination_with_free_lab_slots_is_refused(destination: str) -> None:
    verdict = HandlerLabDispatchGate().handle(
        ModelLabDispatchRequest(
            "Agent",
            {"prompt": f"ROUTE: lane=one dest={destination}"},
            LabPool(3, "healthy"),
        )
    )
    assert not verdict.allowed
    assert verdict.guard == "operator_access_mode_lab_dispatch_gate"
    assert verdict.reason_class == "mac-dispatch-lab-free"
    _assert_dispatch_reason(verdict.reason, free=3, calls=1)


@pytest.mark.parametrize("field", ["prompt", "description", "label"])
def test_agent_routes_in_every_dispatch_text_field_are_checked(field: str) -> None:
    verdict = HandlerLabDispatchGate().handle(
        ModelLabDispatchRequest(
            "Agent", {field: "ROUTE: dest=mac"}, LabPool(1, "healthy")
        )
    )
    assert not verdict.allowed


@pytest.mark.parametrize("free_slots", [0, None])
def test_mac_dispatch_is_allowed_when_pool_is_full_or_unreadable(
    free_slots: int | None,
) -> None:
    verdict = HandlerLabDispatchGate().handle(
        ModelLabDispatchRequest(
            "Agent",
            {"prompt": "ROUTE: dest=mac"},
            LabPool(free_slots, "no capacity known"),
        )
    )
    assert verdict.allowed


@pytest.mark.parametrize(
    "text",
    ["ROUTE: dest=lab", "ROUTE: dest=h201", "Prose mentions dest=mac", "No route", ""],
)
def test_lab_host_and_unrouted_agent_dispatches_pass(text: str) -> None:
    verdict = HandlerLabDispatchGate().handle(
        ModelLabDispatchRequest("Agent", {"prompt": text}, LabPool(3, "healthy"))
    )
    assert verdict.allowed


@pytest.mark.parametrize("tool", ["Bash", "Read", "WebSearch", "OtherTool"])
def test_other_tools_are_untouched_by_dispatch_gate(tool: str) -> None:
    verdict = HandlerLabDispatchGate().handle(
        ModelLabDispatchRequest(
            tool,
            {"prompt": "ROUTE: dest=mac", "script": "ROUTE: dest=mac"},
            LabPool(3, "healthy"),
        )
    )
    assert verdict.allowed


def test_workflow_with_two_agents_counts_only_the_mac_call() -> None:
    script = (
        'Agent(prompt="""ROUTE: dest=lab lane=one\nFirst task""")\n'
        'Agent(prompt="""ROUTE: dest=mac lane=two\nSecond task""")\n'
    )
    verdict = HandlerLabDispatchGate().handle(
        ModelLabDispatchRequest("Workflow", {"script": script}, LabPool(3, "healthy"))
    )
    assert not verdict.allowed
    _assert_dispatch_reason(verdict.reason, free=3, calls=1)


def test_workflow_refusal_counts_multiple_mac_calls() -> None:
    script = (
        "ROUTE: dest=mac lane=one\nROUTE: dest=lab lane=two\nROUTE: dest=mac lane=three"
    )
    verdict = HandlerLabDispatchGate().handle(
        ModelLabDispatchRequest("Workflow", {"script": script}, LabPool(3, "healthy"))
    )
    assert not verdict.allowed
    _assert_dispatch_reason(verdict.reason, free=3, calls=2)


def test_workflow_script_path_is_checked(tmp_path: Path) -> None:
    path = tmp_path / "workflow.py"
    path.write_text('Agent(prompt="ROUTE: dest=mac")\n', encoding="utf-8")
    verdict = HandlerLabDispatchGate().handle(
        ModelLabDispatchRequest(
            "Workflow", {"scriptPath": str(path)}, LabPool(3, "healthy")
        )
    )
    assert not verdict.allowed
    _assert_dispatch_reason(verdict.reason, free=3, calls=1)


@pytest.mark.parametrize("bus_lane", [None, "test-lab-pool"])
def test_pool_reader_invokes_hosts_with_six_second_timeout(
    lab_environ: dict[str, str], bus_lane: str | None
) -> None:
    environ = dict(lab_environ)
    if bus_lane:
        environ["ONEX_LAB_POOL_BUS_LANE"] = bus_lane
    run = Mock(
        return_value=subprocess.CompletedProcess(
            [], 0, json.dumps([_advertisement()]), ""
        )
    )
    pool = read_lab_pool(environ, run=run, now=NOW)
    assert pool.free_slots == 3
    run.assert_called_once()
    args, kwargs = run.call_args
    expected = [
        sys.executable,
        "-m",
        "omnimarket.lab_work.cli",
        "hosts",
        "--omnibase-path",
        environ["OMNI_HOME"],
        "--json",
        "--window",
        "3",
    ]
    if bus_lane:
        expected += ["--bus-lane", bus_lane]
    assert list(args[0]) == expected
    assert kwargs["timeout"] == 6


def test_pool_reader_uses_dispatch_venv_python_when_no_explicit_python(
    tmp_path: Path, lab_environ: dict[str, str]
) -> None:
    python = tmp_path / "dispatch-venv" / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    python.chmod(0o755)
    environ = {
        "OMNI_HOME": lab_environ["OMNI_HOME"],
        "ONEX_DISPATCH_VENV": str(python.parents[1]),
    }
    run = Mock(return_value=subprocess.CompletedProcess([], 0, "[]", ""))
    assert read_lab_pool(environ, run=run, now=NOW).free_slots == 0
    assert run.call_args.args[0][0] == str(python)


@pytest.mark.parametrize("missing", ["OMNI_HOME", "ONEX_LAB_WORK_PYTHON"])
def test_pool_reader_missing_configuration_is_unknown(
    lab_environ: dict[str, str], missing: str
) -> None:
    environ = {key: value for key, value in lab_environ.items() if key != missing}
    run = Mock()
    pool = read_lab_pool(environ, run=run, now=NOW)
    assert pool.free_slots is None
    assert pool.detail
    run.assert_not_called()


def test_pool_reader_missing_python_executable_is_unknown(
    tmp_path: Path, lab_environ: dict[str, str]
) -> None:
    environ = {**lab_environ, "ONEX_LAB_WORK_PYTHON": str(tmp_path / "absent-python")}
    run = Mock(side_effect=FileNotFoundError("absent-python"))
    pool = read_lab_pool(environ, run=run, now=NOW)
    assert pool.free_slots is None
    assert pool.detail


def test_pool_sums_free_slots_on_eligible_hosts(lab_environ: dict[str, str]) -> None:
    pool = _read_pool(
        [
            _advertisement(),
            _advertisement(host_name="lab-worker-h202", running_units=2, max_units=6),
        ],
        lab_environ,
    )
    assert pool.free_slots == 7


@pytest.mark.parametrize(
    "host_name", ["mac", "launching-mac", "extra-mac", "other-launcher"]
)
def test_mac_and_launching_hosts_are_excluded(
    lab_environ: dict[str, str], host_name: str
) -> None:
    environ = {
        **lab_environ,
        "ONEX_LAB_POOL_LAUNCHING_HOSTS": " extra-mac, other-launcher ",
    }
    pool = _read_pool([_advertisement(host_name=host_name)], environ)
    assert pool.free_slots == 0


@pytest.mark.parametrize(
    "overrides",
    [
        {"advertised_at": (NOW - timedelta(seconds=121)).isoformat()},
        {"load1": 5.01, "running_units": 1},
        {"load1": 1.0, "running_units": 6, "max_units": 8},
        {"mem_available_bytes": 4 * 1024**3 - 1},
        {"running_units": 4, "max_units": 4},
        {"running_units": 5, "max_units": 4},
    ],
    ids=[
        "stale",
        "over-load",
        "running-units-add-to-load",
        "low-memory",
        "full",
        "overfull",
    ],
)
def test_ineligible_hosts_contribute_no_slots(
    lab_environ: dict[str, str], overrides: dict[str, Any]
) -> None:
    assert _read_pool([_advertisement(**overrides)], lab_environ).free_slots == 0


def test_freshness_load_and_memory_thresholds_are_inclusive(
    lab_environ: dict[str, str],
) -> None:
    host = _advertisement(
        advertised_at=(NOW - timedelta(seconds=120)).isoformat(),
        load1=4.0,
        running_units=2,
        max_units=5,
        mem_available_bytes=4 * 1024**3,
    )
    assert _read_pool([host], lab_environ).free_slots == 3


@pytest.mark.parametrize("failure", ["timeout", "nonzero", "bad-json"])
def test_unreadable_pool_is_unknown_instead_of_refused(
    lab_environ: dict[str, str], failure: str
) -> None:
    if failure == "timeout":
        run = Mock(side_effect=subprocess.TimeoutExpired("hosts", 6))
    else:
        run = Mock(
            return_value=subprocess.CompletedProcess(
                [],
                1 if failure == "nonzero" else 0,
                "[]" if failure == "nonzero" else "not JSON",
                "hosts unavailable",
            )
        )
    pool = read_lab_pool(lab_environ, run=run, now=NOW)
    assert pool.free_slots is None
    assert pool.detail


@pytest.mark.parametrize("name", ["local_cli", "remote_bridge", "headless_sdk"])
@pytest.mark.parametrize("agent_id", ["", "subagent-lane"])
def test_pre_tool_refuses_mac_agents_in_every_session(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    name: str,
    agent_id: str,
) -> None:
    environ = _environ(name, tmp_path)
    _, payload = _sample(name)
    pool_reader = Mock(return_value=LabPool(3, "healthy"))
    monkeypatch.setattr(cli, "read_lab_pool", pool_reader)
    payload = {
        **payload,
        "agent_id": agent_id,
        "tool_name": "Agent",
        "tool_input": {"prompt": "ROUTE: dest=mac"},
    }
    assert cli.on_pre_tool(payload, environ) == 2
    output = capsys.readouterr()
    assert output.out == ""
    _assert_dispatch_reason(output.err, free=3, calls=1)
    pool_reader.assert_called_once_with(environ)


@pytest.mark.parametrize(
    ("destination", "free_slots"), [("lab", 3), ("h201", 3), ("mac", 0), ("mac", None)]
)
def test_pre_tool_allows_lab_routes_and_unknown_or_full_pool(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    destination: str,
    free_slots: int | None,
) -> None:
    environ = _environ("local_cli", tmp_path)
    _, payload = _sample("local_cli")
    monkeypatch.setattr(
        cli, "read_lab_pool", Mock(return_value=LabPool(free_slots, "test pool"))
    )
    payload = {
        **payload,
        "tool_name": "Agent",
        "tool_input": {"prompt": f"ROUTE: dest={destination}"},
    }
    assert cli.on_pre_tool(payload, environ) == 0
    output = capsys.readouterr()
    assert output.out == output.err == ""


@pytest.mark.parametrize("use_path", [False, True])
def test_pre_tool_checks_workflow_scripts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    use_path: bool,
) -> None:
    environ = _environ("local_cli", tmp_path)
    _, payload = _sample("local_cli")
    monkeypatch.setattr(cli, "read_lab_pool", Mock(return_value=LabPool(3, "healthy")))
    script = 'Agent(prompt="ROUTE: dest=lab")\nAgent(prompt="ROUTE: dest=mac")\n'
    path = tmp_path / "workflow.py"
    path.write_text(script, encoding="utf-8")
    tool_input = {"scriptPath": str(path)} if use_path else {"script": script}
    assert (
        cli.on_pre_tool(
            {**payload, "tool_name": "Workflow", "tool_input": tool_input}, environ
        )
        == 2
    )
    _assert_dispatch_reason(capsys.readouterr().err, free=3, calls=1)


@pytest.mark.parametrize(
    ("tool", "tool_input"),
    [
        ("Bash", {"command": "echo hello", "prompt": "ROUTE: dest=mac"}),
        ("Read", {"prompt": "ROUTE: dest=mac"}),
    ],
)
def test_pre_tool_other_tools_do_not_read_lab_pool(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tool: str,
    tool_input: dict[str, str],
) -> None:
    environ = _environ("local_cli", tmp_path)
    _, payload = _sample("local_cli")
    reader = Mock(side_effect=AssertionError("unrelated tool queried the lab"))
    monkeypatch.setattr(cli, "read_lab_pool", reader)
    assert (
        cli.on_pre_tool(
            {**payload, "tool_name": tool, "tool_input": tool_input}, environ
        )
        == 0
    )
    reader.assert_not_called()
    output = capsys.readouterr()
    assert output.out == output.err == ""


@pytest.mark.parametrize(
    ("tool", "tool_input"),
    [
        ("Agent", {"prompt": "ROUTE: band=B3 dest=lab model=sonnet"}),
        ("Agent", {"prompt": "no route line at all"}),
        ("Workflow", {"script": "agent('x', {label: 'ROUTE: dest=h201'})"}),
    ],
)
def test_pre_tool_agent_without_a_mac_route_does_not_read_lab_pool(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    tool: str,
    tool_input: dict[str, str],
) -> None:
    environ = _environ("local_cli", tmp_path)
    _, payload = _sample("local_cli")
    reader = Mock(side_effect=AssertionError("a lab-routed call queried the lab"))
    monkeypatch.setattr(cli, "read_lab_pool", reader)
    assert (
        cli.on_pre_tool(
            {**payload, "tool_name": tool, "tool_input": tool_input}, environ
        )
        == 0
    )
    reader.assert_not_called()


def test_real_cli_refuses_mac_agent_using_fake_pool_python(tmp_path: Path) -> None:
    fake_python = tmp_path / "fake-python"
    host = _advertisement(
        advertised_at=datetime.now(UTC).isoformat(), host_name="isolated-lab-worker"
    )
    fake_python.write_text(
        f"#!{sys.executable}\nimport json, sys\n"
        "assert sys.argv[1:4] == ['-m', 'omnimarket.lab_work.cli', 'hosts']\n"
        "assert '--bus-lane' not in sys.argv\n"
        f"print(json.dumps({[host]!r}))\n",
        encoding="utf-8",
    )
    fake_python.chmod(0o755)
    environ = _environ(
        "local_cli",
        tmp_path / "state",
        ONEX_LAB_WORK_PYTHON=str(fake_python),
        OMNI_HOME=str(tmp_path),
    )
    _, payload = _sample("local_cli")
    payload = {
        **payload,
        "tool_name": "Agent",
        "tool_input": {"prompt": "ROUTE: dest=Mac"},
    }
    result = _run("pre-tool", payload, environ)
    assert result.returncode == 2, (result.stdout, result.stderr)
    assert result.stdout == ""
    _assert_dispatch_reason(result.stderr, free=3, calls=1)


def test_installer_registers_workflow_and_agent_in_pre_tool_matcher(
    tmp_path: Path,
) -> None:
    expected = (
        "^(Bash|WebFetch|WebSearch|PushNotification|SendUserMessage|Workflow|Agent)$"
    )
    registration = next(row for row in REGISTRATIONS if row[0] == "PreToolUse")
    assert registration[1] == expected
    settings: dict[str, Any] = {}
    install_operator_hooks(settings, tmp_path / "hooks", sys.executable)
    assert settings["hooks"]["PreToolUse"][0]["matcher"] == expected
