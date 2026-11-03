# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Handler dependencies are declared in the contract and bound by the lane overlay (OMN-21595).

AC1 is a test over the syntax tree of both node directories. AC2 runs one test body
under an overlay that binds in-memory dependencies and one that binds the durable ones.
AC3 refuses a contract whose dependency the overlay does not bind before anything is
built or subscribed. AC4 reads the four overlay settings from the overlay file alone.
"""

from __future__ import annotations

import ast
import asyncio
import inspect
import json
import sys
from collections.abc import Callable, Generator, Mapping
from pathlib import Path

import pytest
import yaml
from omnibase_core.enums.enum_dependency_binding_refusal_reason import (
    EnumDependencyBindingRefusalReason,
)
from omnibase_core.event_bus.event_bus_inmemory import EventBusInmemory
from omnibase_core.models.nodes.handler_dependencies.model_overlay_bindings import (
    ModelOverlayBindings,
)
from pydantic import ValidationError

from omnimarket.handlers.container.container_overlay_dependencies import (
    FACTORIES,
    OVERLAY_DOCUMENT_PROTOCOL,
    PROCESS_ENVIRONMENT_PROTOCOL,
    DependencyUnboundError,
    bind_overlay,
    declared_handler_dependencies,
    load_overlay_bindings,
    require_dependencies,
)
from omnimarket.nodes.node_llm_delegation_call_effect.handlers import transport
from omnimarket.nodes.node_llm_delegation_call_effect.handlers.handler_claude_credential_probe import (
    HandlerClaudeCredentialProbe,
)
from omnimarket.nodes.node_llm_delegation_call_effect.handlers.handler_codex_harness_attempt import (
    HandlerCodexHarnessAttempt,
)
from omnimarket.nodes.node_llm_delegation_call_effect.handlers.handler_delegation_target import (
    HandlerDelegationTarget,
    load_delegation_config,
)
from omnimarket.nodes.node_llm_delegation_call_effect.models.model_codex_harness_attempt import (
    ModelCodexHarnessRequest,
)
from tests.overlay_bound_support import durable_overlay, in_memory_overlay

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[4]
NODE_DIRECTORIES = (
    REPO_ROOT / "src/omnimarket/nodes/node_llm_delegation_call_effect",
    REPO_ROOT / "src/omnimarket/nodes/node_delegation_orchestrator",
)
PACKAGE = "omnimarket.nodes.node_llm_delegation_call_effect"
_ENVIRONMENT_ATTRIBUTES = frozenset({"environ", "environb", "getenv", "getenvb"})


def environment_accesses(source: str) -> list[tuple[int, str]]:
    """Executable ``os.environ``/``os.getenv`` uses in ``source``, by line, from the syntax tree."""
    tree = ast.parse(source)
    os_names: set[str] = set()
    direct: set[str] = set()
    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            os_names |= {a.asname or a.name for a in node.names if a.name == "os"}
        elif isinstance(node, ast.ImportFrom) and node.module == "os":
            for alias in node.names:
                if alias.name in _ENVIRONMENT_ATTRIBUTES:
                    direct.add(alias.asname or alias.name)
                    found.append((node.lineno, f"from os import {alias.name}"))
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Attribute)
            and node.attr in _ENVIRONMENT_ATTRIBUTES
            and isinstance(node.value, ast.Name)
            and node.value.id in os_names
        ):
            found.append((node.lineno, ast.unparse(node)))
        elif isinstance(node, ast.Name) and node.id in direct:
            found.append((node.lineno, node.id))
    return sorted(set(found))


def test_the_checker_finds_an_access_and_ignores_a_docstring() -> None:
    """Positive control for AC1: the walk sees code, never prose."""
    code = 'import os\nx = os.environ.get("A")\ny = os.getenv("B")\n'
    assert [what for _, what in environment_accesses(code)] == [
        "os.environ",
        "os.getenv",
    ]
    aliased = "import os as o\nfrom os import environ as e\nz = o.environ\nw = e\n"
    assert len(environment_accesses(aliased)) >= 3
    prose = (
        '"""Reads os.environ and os.getenv in prose."""\n# os.environ["A"]\nimport os\n'
    )
    assert environment_accesses(prose) == []


def test_no_executable_environment_access_in_either_node() -> None:
    """AC1: both node directories, every module, from the syntax tree."""
    modules = [p for d in NODE_DIRECTORIES for p in sorted(d.rglob("*.py"))]
    assert len(modules) > 40
    offenders = {
        f"{path.relative_to(REPO_ROOT)}:{line}": what
        for path in modules
        for line, what in environment_accesses(path.read_text())
    }
    assert offenders == {}


# ---- AC2: one test body, two bindings -------------------------------------------------

_ENGINE = """import json, os, pathlib, sys
args = sys.argv
sys.stdin.read()
pathlib.Path(args[args.index("-o") + 1]).write_text(os.environ["LANE_OVERLAY_MARKER"])
print(json.dumps({"type": "turn.completed", "usage": {"input_tokens": 1, "output_tokens": 1}}))
"""


def _engine(tmp_path: Path) -> Path:
    path = tmp_path / "engine"
    path.write_text(f"#!{sys.executable}\n" + _ENGINE)
    path.chmod(0o700)
    return path


def _policy_document(tmp_path: Path, timeout: int) -> str:
    return (
        f"codex_harness:\n  executable: {_engine(tmp_path)}\n  workdir: {tmp_path}\n"
        f"  max_timeout_seconds: {timeout}\n"
    )


def _bind_in_memory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, timeout: int
) -> None:
    bind_overlay(
        in_memory_overlay(
            environment={"LANE_OVERLAY_MARKER": "from-the-overlay"},
            documents={
                "codex_harness": _policy_document(tmp_path, timeout),
                "transport": "runtime_profile: local_macos_claude_hooks\n",
                "delegation_adapters": "call_sites: {}\n",
            },
        )
    )


def _bind_durable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, timeout: int
) -> None:
    monkeypatch.setenv("LANE_OVERLAY_MARKER", "from-the-overlay")
    paths = {}
    for name, text in {
        "codex_harness": _policy_document(tmp_path, timeout),
        "transport": "runtime_profile: local_macos_claude_hooks\n",
        "delegation_adapters": "call_sites: {}\n",
    }.items():
        paths[name] = str(tmp_path / f"{name}.yaml")
        Path(paths[name]).write_text(text)
    bind_overlay(durable_overlay(document_paths=paths))


BINDINGS: dict[str, Callable[[Path, pytest.MonkeyPatch, int], None]] = {
    "in_memory": _bind_in_memory,
    "durable": _bind_durable,
}


@pytest.fixture(params=sorted(BINDINGS))
def bind_lane(
    request: pytest.FixtureRequest, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Generator[Callable[[int], None], None, None]:
    """Binds the overlay under test; the test bodies below never learn which one it is."""

    def bind(timeout: int = 10) -> None:
        BINDINGS[request.param](tmp_path, monkeypatch, timeout)

    yield bind
    bind_overlay(None)


def test_the_child_process_gets_the_environment_the_overlay_binds(
    bind_lane: Callable[[int], None],
) -> None:
    bind_lane()
    receipt = HandlerCodexHarnessAttempt().handle(
        ModelCodexHarnessRequest(prompt="p", model="m", timeout_seconds=3)
    )
    assert receipt.exit_cause == "completed"
    assert receipt.answer == "from-the-overlay"


def test_a_declared_value_changes_the_behaviour_without_a_code_change(
    bind_lane: Callable[[int], None],
) -> None:
    """Falsifier: the timeout ceiling is a document value; changing it flips the outcome."""
    attempt = ModelCodexHarnessRequest(prompt="p", model="m", timeout_seconds=7)
    bind_lane(10)
    assert HandlerCodexHarnessAttempt().handle(attempt).exit_cause == "completed"
    bind_lane(5)
    with pytest.raises(ValueError, match="timeout"):
        HandlerCodexHarnessAttempt().handle(attempt)


def test_the_transport_profile_is_the_overlay_document_value(
    bind_lane: Callable[[int], None],
) -> None:
    bind_lane()
    assert transport.resolve_runtime_profile() == "local_macos_claude_hooks"
    assert transport.uses_lan_curl_transport() is True
    bind_overlay(None)
    assert transport.resolve_runtime_profile() == "main"
    assert transport.uses_lan_curl_transport() is False


def test_the_delegation_adapter_config_is_the_overlay_document(
    bind_lane: Callable[[int], None],
) -> None:
    bind_lane()
    config = load_delegation_config()
    assert config["call_sites"] == {}
    handler = HandlerDelegationTarget(event_bus=None)
    assert handler._config["call_sites"] == {}


def test_the_probe_scrubs_from_the_environment_the_overlay_binds(
    bind_lane: Callable[[int], None],
) -> None:
    bind_lane()
    probe = HandlerClaudeCredentialProbe()
    child = probe._environment.environment()
    assert child["LANE_OVERLAY_MARKER"] == "from-the-overlay"


def test_the_shared_bodies_do_not_branch_on_the_binding() -> None:
    """Falsifier for AC2: a test body that names the binding kind is a branch on it."""
    tree = ast.parse(Path(__file__).read_text())
    shared = {
        node.name: node
        for node in tree.body
        if isinstance(node, ast.FunctionDef)
        and "bind_lane" in [a.arg for a in node.args.args]
    }
    assert len(shared) == 5
    for name, node in shared.items():
        names = {n.id for n in ast.walk(node) if isinstance(n, ast.Name)}
        assert not names & {"BINDINGS", "request", "in_memory", "durable"}, name
        assert not any(isinstance(n, ast.If | ast.IfExp) for n in ast.walk(node)), name
    assert inspect.getsource(bind_lane).count("request.param") == 1


# ---- AC3: DEPENDENCY_UNBOUND before any subscription ----------------------------------


def test_the_contract_declares_both_dependencies() -> None:
    assert declared_handler_dependencies(PACKAGE) == (
        PROCESS_ENVIRONMENT_PROTOCOL,
        OVERLAY_DOCUMENT_PROTOCOL,
    )


def test_an_unbound_dependency_is_refused_before_any_factory_or_subscription() -> None:
    built: list[str] = []

    def recording(name: str) -> Callable[[Mapping[str, str]], object]:
        def factory(config: Mapping[str, str]) -> object:
            built.append(name)
            return FACTORIES[name](config)

        return factory

    factories = {name: recording(name) for name in FACTORIES}
    full = in_memory_overlay()
    only_environment = ModelOverlayBindings(
        dependencies=(full.dependencies[0],),
        announcer_bus=full.announcer_bus,
        catalog_store=full.catalog_store,
        lazy_start_budget_seconds=full.lazy_start_budget_seconds,
    )
    bus = EventBusInmemory(environment="test", group="overlay-bound")
    result = only_environment.build(declared_handler_dependencies(PACKAGE), factories)
    assert (
        result.refusal_reason is EnumDependencyBindingRefusalReason.DEPENDENCY_UNBOUND
    )
    assert result.unbound == (OVERLAY_DOCUMENT_PROTOCOL,)
    assert built == []
    assert asyncio.run(bus.get_topics()) == []

    bind_overlay(only_environment)
    try:
        with pytest.raises(DependencyUnboundError, match="dependency_unbound"):
            assert HandlerDelegationTarget(event_bus=bus)._environment
        with pytest.raises(DependencyUnboundError):
            HandlerCodexHarnessAttempt()
        with pytest.raises(DependencyUnboundError):
            HandlerClaudeCredentialProbe()
    finally:
        bind_overlay(None)
    assert asyncio.run(bus.get_topics()) == []


def test_no_overlay_installed_refuses_every_declared_dependency() -> None:
    bind_overlay(None)
    with pytest.raises(DependencyUnboundError) as refused:
        require_dependencies(declared_handler_dependencies(PACKAGE))
    assert refused.value.built.unbound == declared_handler_dependencies(PACKAGE)


# ---- AC4: the four settings are read from the overlay and from nowhere else -----------


def _overlay_text(
    budget: float, reaper_days: float | None, bus: str, store: str
) -> str:
    document: dict[str, object] = {
        "dependencies": [],
        "announcer_bus": {"protocol": "announcer_bus", "implementation": bus},
        "catalog_store": {"protocol": "catalog_store", "implementation": store},
        "lazy_start_budget_seconds": budget,
    }
    if reaper_days is not None:
        document["reaper_flag_to_delete_days"] = reaper_days
    return yaml.safe_dump(document)


def test_the_four_settings_follow_the_overlay_file(tmp_path: Path) -> None:
    """Falsifier: a constant in code would not move when the overlay file does."""
    first = tmp_path / "first.yaml"
    first.write_text(_overlay_text(12.5, 3.0, "bus-a", "store-a"))
    second = tmp_path / "second.yaml"
    second.write_text(_overlay_text(99.0, 41.0, "bus-b", "store-b"))
    one, two = load_overlay_bindings(first), load_overlay_bindings(second)
    assert (
        one.announcer_bus.implementation,
        one.catalog_store.implementation,
        one.lazy_start_budget_seconds,
        one.reaper_flag_to_delete_days,
    ) == ("bus-a", "store-a", 12.5, 3.0)
    assert (
        two.announcer_bus.implementation,
        two.catalog_store.implementation,
        two.lazy_start_budget_seconds,
        two.reaper_flag_to_delete_days,
    ) == ("bus-b", "store-b", 99.0, 41.0)


def test_the_four_settings_have_no_default_in_code(tmp_path: Path) -> None:
    """An overlay that leaves out a setting is refused; the reaper interval stays unset."""
    without_budget = tmp_path / "without_budget.yaml"
    document = yaml.safe_load(_overlay_text(1.0, None, "b", "s"))
    del document["lazy_start_budget_seconds"]
    without_budget.write_text(yaml.safe_dump(document))
    with pytest.raises(ValidationError):
        load_overlay_bindings(without_budget)
    without_bus = tmp_path / "without_bus.yaml"
    document = yaml.safe_load(_overlay_text(1.0, None, "b", "s"))
    del document["announcer_bus"]
    without_bus.write_text(yaml.safe_dump(document))
    with pytest.raises(ValidationError):
        load_overlay_bindings(without_bus)
    with_none = tmp_path / "with_none.yaml"
    with_none.write_text(_overlay_text(1.0, None, "b", "s"))
    assert load_overlay_bindings(with_none).reaper_flag_to_delete_days is None


def test_a_lane_overlay_file_round_trips_through_the_cli_loader(tmp_path: Path) -> None:
    overlay = durable_overlay(
        lazy_start_budget_seconds=7.0, reaper_flag_to_delete_days=2.0
    )
    path = tmp_path / "lane.yaml"
    path.write_text(json.dumps(overlay.model_dump(mode="json")))
    assert load_overlay_bindings(path) == overlay
