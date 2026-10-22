# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-21289 AC1: no harness is invoked outside a handler of the delegation effect node.

The falsifier is a fixture script that calls the codex CLI directly: the check
refuses it. Fixtures are stored as ``.txt`` so no gate counts them as live call
sites, and each test plants one under its real name in a temporary repository. The same text inside the delegation effect node's handlers is
admitted, and the repository itself passes only through the shrink-only
baseline measured when the check landed.
"""

from __future__ import annotations

import importlib
import json
import shutil
import subprocess
import sys
from importlib.metadata import entry_points
from pathlib import Path

import pytest
import yaml
from omnibase_core.enums.enum_workflow_result import EnumWorkflowResult

import omnimarket.nodes.node_harness_invocation_check_effect as node_package
from omnimarket.nodes.node_harness_invocation_check_effect.handlers.handler_harness_invocation_check import (
    HandlerHarnessInvocationCheck,
    load_policy,
)
from omnimarket.nodes.node_harness_invocation_check_effect.models.model_harness_invocation_check import (
    EnumHarnessInvocationKind,
    ModelHarnessInvocationBaselineEntry,
    ModelHarnessInvocationCheckRequest,
    ModelHarnessInvocationCheckResult,
)
from tests.runtime_local_compat import RuntimeLocal

pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[3]
NODE_DIR = Path(node_package.__file__).parent
FIXTURES = ROOT / "tests" / "fixtures" / "harness_invocation"
COMMAND_TOPIC = "onex.cmd.omnimarket.harness-invocation-check-requested.v1"
TERMINAL_TOPIC = "onex.evt.omnimarket.harness-invocation-check-completed.v1"
POLICY = load_policy()
SANCTIONED = POLICY.sanctioned_handler_dir

# The sites measured on dev when the check landed. The contract baseline may
# only lose entries or lower counts against this list; it never gains one.
MEASURED_BASELINE = {
    ("benchmarks/delegation_false_pass_growth/grade.sh", "cli_exec", "claude"): 1,
    (
        "src/omnimarket/nodes/node_demo_fanout_orchestrator/handlers/handler_fanout.py",
        "cli_exec",
        "claude",
    ): 1,
    (
        "src/omnimarket/nodes/node_lab_claude_token_effect/handlers/handler_lab_claude_token.py",
        "cli_exec",
        "claude",
    ): 1,
    (
        "src/omnimarket/nodes/node_overnight/handlers/handler_overnight.py",
        "cli_exec",
        "claude",
    ): 1,
    (
        "src/omnimarket/nodes/node_platform_readiness/handlers/handler_platform_readiness.py",
        "cli_exec",
        "claude",
    ): 1,
    (
        "src/omnimarket/nodes/node_session_orchestrator/handlers/handler_session_orchestrator.py",
        "cli_exec",
        "claude",
    ): 1,
}


def _check(
    repo_root: Path, *paths: str, handler: HandlerHarnessInvocationCheck | None = None
) -> ModelHarnessInvocationCheckResult:
    return (handler or HandlerHarnessInvocationCheck()).handle(
        ModelHarnessInvocationCheckRequest(repo_root=str(repo_root), paths=paths)
    )


def _plant(tmp_path: Path, fixture: str, rel: str | None = None) -> tuple[Path, str]:
    """Copy a ``.txt`` fixture into a temporary repository under its script name."""
    rel = rel or f"scripts/{fixture.removesuffix('.txt')}"
    target = tmp_path / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy(FIXTURES / fixture, target)
    return tmp_path, rel


def test_check_refuses_a_fixture_script_calling_the_codex_cli_directly(
    tmp_path: Path,
) -> None:
    repo, rel = _plant(tmp_path, "codex_direct.py.txt")
    result = _check(repo, rel)
    assert not result.admitted
    assert result.exit_code == 1
    assert [(s.kind, s.target) for s in result.refused] == [
        (EnumHarnessInvocationKind.CLI_EXEC, "codex")
    ]
    assert result.refused[0].path == rel
    assert any("codex_direct.py" in line for line in result.output_lines)


@pytest.mark.parametrize(
    ("name", "kind", "target"),
    [
        ("codex_direct.sh.txt", EnumHarnessInvocationKind.CLI_EXEC, "codex"),
        ("openrouter_direct.py.txt", EnumHarnessInvocationKind.HTTP, "openrouter.ai"),
        ("glm_direct.sh.txt", EnumHarnessInvocationKind.HTTP, "api.z.ai"),
    ],
)
def test_check_refuses_other_direct_harness_invocations(
    tmp_path: Path, name: str, kind: EnumHarnessInvocationKind, target: str
) -> None:
    repo, rel = _plant(tmp_path, name)
    result = _check(repo, rel)
    assert not result.admitted
    assert [(s.kind, s.target) for s in result.refused] == [(kind, target)]


def test_check_admits_a_file_that_only_names_harnesses(tmp_path: Path) -> None:
    repo, rel = _plant(tmp_path, "clean.py.txt")
    result = _check(repo, rel)
    assert result.admitted
    assert result.sites == ()


def test_same_invocation_inside_the_delegation_effect_handlers_is_admitted(
    tmp_path: Path,
) -> None:
    handler = HandlerHarnessInvocationCheck(POLICY.model_copy(update={"baseline": ()}))
    repo, _ = _plant(tmp_path, "codex_direct.py.txt", f"{SANCTIONED}/handler_codex.py")
    result = _check(repo, handler=handler)
    assert result.admitted
    assert [s.target for s in result.sites] == ["codex"]


def test_full_scan_refuses_a_new_site_in_a_new_script(tmp_path: Path) -> None:
    handler = HandlerHarnessInvocationCheck(POLICY.model_copy(update={"baseline": ()}))
    repo, rel = _plant(tmp_path, "codex_direct.py.txt")
    result = _check(repo, handler=handler)
    assert not result.admitted
    assert [s.path for s in result.refused] == [rel]


def test_baseline_count_admits_no_second_site_in_a_baselined_file(
    tmp_path: Path,
) -> None:
    repo, rel = _plant(tmp_path, "codex_direct.sh.txt")
    path = repo / rel
    path.write_text(path.read_text() + 'codex exec "again"\n')
    entry = ModelHarnessInvocationBaselineEntry(
        path=rel, kind=EnumHarnessInvocationKind.CLI_EXEC, target="codex", count=1
    )
    handler = HandlerHarnessInvocationCheck(
        POLICY.model_copy(update={"baseline": (entry,)})
    )
    result = _check(repo, handler=handler)
    assert not result.admitted
    assert len(result.refused) == 1


def test_full_scan_refuses_a_stale_baseline_entry(tmp_path: Path) -> None:
    entry = ModelHarnessInvocationBaselineEntry(
        path="scripts/gone.sh",
        kind=EnumHarnessInvocationKind.CLI_EXEC,
        target="codex",
        count=1,
    )
    handler = HandlerHarnessInvocationCheck(
        POLICY.model_copy(update={"baseline": (entry,)})
    )
    (tmp_path / "src").mkdir()
    result = _check(tmp_path, handler=handler)
    assert not result.admitted
    assert result.stale_baseline == (entry,)


def test_repository_admits_no_harness_invocation_outside_the_baseline() -> None:
    result = _check(ROOT)
    assert result.admitted, "\n".join(result.output_lines)
    assert any(s.path.startswith(SANCTIONED + "/") for s in result.sites)


def test_baseline_only_shrinks_from_the_measured_list() -> None:
    for entry in POLICY.baseline:
        key = (entry.path, entry.kind.value, entry.target)
        assert key in MEASURED_BASELINE, f"new baseline entry {key}"
        assert entry.count <= MEASURED_BASELINE[key], f"baseline count grew {key}"
    assert SANCTIONED == "src/omnimarket/nodes/node_llm_delegation_call_effect/handlers"
    assert set(POLICY.cli_targets) >= {"codex", "claude"}
    assert set(POLICY.http_hosts) >= {"openrouter.ai", "api.z.ai"}


def test_contract_declares_topics_models_handler_and_entry_point() -> None:
    contract = yaml.safe_load((NODE_DIR / "contract.yaml").read_text())
    assert contract["node_type"] == "effect"
    assert contract["runtime_dispatch"]["command_topic"] == COMMAND_TOPIC
    assert contract["event_bus"]["subscribe_topics"] == [COMMAND_TOPIC]
    assert contract["event_bus"]["publish_topics"] == [TERMINAL_TOPIC]
    assert contract["terminal_event"] == TERMINAL_TOPIC
    assert contract["metadata"]["related_tickets"] == ["OMN-21289"]
    binding = contract["handler"]
    handler_type = getattr(importlib.import_module(binding["module"]), binding["class"])
    assert issubclass(node_package.NodeHarnessInvocationCheckEffect, handler_type)
    for side, model in (
        ("input_model", ModelHarnessInvocationCheckRequest),
        ("output_model", ModelHarnessInvocationCheckResult),
    ):
        module = importlib.import_module(contract[side]["module"])
        assert getattr(module, contract[side]["name"]) is model
    registered = {e.name: e for e in entry_points(group="onex.nodes")}
    assert registered["node_harness_invocation_check_effect"].load() is node_package


def test_cli_exits_one_on_the_codex_fixture(tmp_path: Path) -> None:
    repo, rel = _plant(tmp_path, "codex_direct.py.txt")
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "omnimarket.nodes.node_harness_invocation_check_effect",
            "--repo-root",
            str(repo),
            rel,
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 1
    assert "cli_exec codex" in proc.stdout


@pytest.mark.asyncio
async def test_golden_chain_over_bus_refuses_the_codex_fixture(tmp_path: Path) -> None:
    repo, rel = _plant(tmp_path / "repo", "codex_direct.py.txt")
    input_path = tmp_path / "request.json"
    input_path.write_text(json.dumps({"repo_root": str(repo), "paths": [rel]}))
    runtime = RuntimeLocal(
        workflow_path=NODE_DIR / "contract.yaml",
        input_path=input_path,
        state_root=tmp_path / "state",
        backend_overrides={"event_bus": "inmemory"},
        timeout=10,
    )
    await runtime.run_async()
    result = runtime.handler_result
    assert isinstance(result, ModelHarnessInvocationCheckResult)
    assert not result.admitted
    assert [s.target for s in result.refused] == ["codex"]


@pytest.mark.asyncio
async def test_error_chain_over_bus_fails_without_result(tmp_path: Path) -> None:
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"repo_root": str(ROOT), "surprise": 1}))
    refused = RuntimeLocal(
        workflow_path=NODE_DIR / "contract.yaml",
        input_path=bad,
        state_root=tmp_path / "state",
        backend_overrides={"event_bus": "inmemory"},
        timeout=10,
    )
    assert await refused.run_async() is EnumWorkflowResult.FAILED
    assert refused.handler_result is None
