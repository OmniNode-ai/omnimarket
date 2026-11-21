# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""File-level closure: a change to any module selects its reverse import closure by file.

The rule replaces the module-level escalations (``shared_module``, ``unmapped_module``,
``threshold_modules``) at the policy's ``file_level_closure_stage``. It stays inactive at the
shipped ``selection_stage`` of 0, so no pull request runs a different selection until a
stage-advance change. Every fixture tree below carries real import statements; the closure is read
from them, not from a list.
"""

from __future__ import annotations

import asyncio
import importlib
from importlib.resources import files
from pathlib import Path
from typing import Any

import pytest
import yaml
from omnibase_core.event_bus.event_bus_inmemory import EventBusInmemory
from pydantic import ValidationError

from omnimarket.handlers.adapter_test_selection_policy_file import load_policy
from omnimarket.nodes.node_test_selection_selector_compute.adapters.adjacency_file import (
    load_adjacency_map,
)
from omnimarket.nodes.node_test_selection_selector_compute.handlers.handler_file_level_closure import (
    HandlerFileLevelClosure,
    legacy_module_escalation,
    resolve_source_module,
)
from omnimarket.nodes.node_test_selection_selector_compute.handlers.handler_test_path_resolution import (
    _import_graph,
)
from omnimarket.nodes.node_test_selection_selector_compute.models.model_file_level_closure import (
    EnumClosureEscalation,
    EnumRootConftestEdge,
    ModelFileLevelClosureRequest,
    ModelFileLevelClosureResult,
)
from omnimarket.nodes.node_test_selection_selector_compute.models.model_test_selection_record import (
    EnumFullSuiteReason,
)
from scripts.ci.detect_test_paths import REPO_ROOT, compute_selection

pytestmark = pytest.mark.unit

ADJACENCY_PATH = REPO_ROOT / "scripts" / "ci" / "test_selection_adjacency.yaml"
ADJACENCY = load_adjacency_map(ADJACENCY_PATH)


def _write(root: Path, rel: str, text: str = "") -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _tree(root: Path) -> None:
    """A small omnimarket: two models, a handler module, one node, and tests that import them."""
    for package in (
        "src/omnimarket",
        "src/omnimarket/models",
        "src/omnimarket/handlers",
        "src/omnimarket/nodes",
        "src/omnimarket/nodes/node_a",
        "tests",
    ):
        _write(root, f"{package}/__init__.py")
    _write(root, "src/omnimarket/models/model_m.py", "class M: ...\n")
    _write(root, "src/omnimarket/models/model_n.py", "class N: ...\n")
    _write(root, "src/omnimarket/handlers/adapter_h.py", "H = 1\n")
    _write(
        root,
        "src/omnimarket/nodes/node_a/handler.py",
        "from omnimarket.models.model_m import M\n\n\ndef run() -> type[M]:\n    return M\n",
    )
    _write(root, "tests/gates/test_gate.py", "def test_g() -> None: ...\n")
    _write(
        root,
        "tests/models/test_m.py",
        "from omnimarket.models.model_m import M\n\n\ndef test_m() -> None:\n    assert M\n",
    )
    _write(
        root,
        "tests/models/test_n.py",
        "from omnimarket.models.model_n import N\n\n\ndef test_n() -> None:\n    assert N\n",
    )
    _write(
        root,
        "tests/nodes/node_a/test_a.py",
        "from omnimarket.nodes.node_a.handler import run\n\n\ndef test_a() -> None:\n    assert run\n",
    )
    _write(root, "tests/test_bystander.py", "def test_b() -> None: ...\n")


def _close(
    root: Path,
    *changed: str,
    edge: EnumRootConftestEdge = EnumRootConftestEdge.CUT,
) -> ModelFileLevelClosureResult:
    _import_graph.cache_clear()  # a test edits its tree between two calls
    return HandlerFileLevelClosure().handle(
        ModelFileLevelClosureRequest(
            changed_files=changed,
            adjacency=ADJACENCY,
            repo_root=str(root),
            root_conftest_edge=edge,
        )
    )


def test_file_level_closure_models_change_selects_reverse_closure_not_suite(
    tmp_path: Path,
) -> None:
    _tree(tmp_path)
    result = _close(tmp_path, "src/omnimarket/models/model_m.py")
    assert result.escalation is None
    assert "tests/models/test_m.py" in result.selected_paths
    # the node that imports the model, and so its tests, are in the closure
    assert "tests/nodes/node_a/" in result.selected_paths
    assert "tests/models/test_n.py" not in result.selected_paths
    assert "tests/test_bystander.py" not in result.selected_paths
    assert "tests/" not in result.selected_paths


def test_file_level_closure_file_directly_under_handlers_resolves(
    tmp_path: Path,
) -> None:
    """Today ``handlers`` has no adjacency entry, so a change there is ``unmapped_module``."""
    _tree(tmp_path)
    _write(
        tmp_path,
        "tests/test_h.py",
        "from omnimarket.handlers.adapter_h import H\n\n\ndef test_h() -> None:\n    assert H\n",
    )
    result = _close(tmp_path, "src/omnimarket/handlers/adapter_h.py")
    assert result.escalation is None
    assert result.selected_paths == ("tests/test_h.py",)


def test_file_level_closure_six_changed_modules_do_not_escalate_by_count(
    tmp_path: Path,
) -> None:
    _tree(tmp_path)
    changed = (
        "src/omnimarket/models/model_m.py",
        "src/omnimarket/models/model_n.py",
        "src/omnimarket/handlers/adapter_h.py",
        "src/omnimarket/nodes/node_a/handler.py",
        "src/omnimarket/models/__init__.py",
        "src/omnimarket/nodes/__init__.py",
    )
    assert len(changed) == 6
    result = _close(tmp_path, *changed)
    assert result.escalation is None
    assert result.selected_paths


def test_file_level_closure_is_inactive_below_the_stage_and_policy_ships_stage_zero() -> (
    None
):
    policy = load_policy()
    assert policy.selection_stage == 0
    assert policy.file_level_closure_stage == 3
    assert policy.root_conftest_edge is EnumRootConftestEdge.CUT
    # stage 0 keeps every escalation the closure replaces
    assert (
        legacy_module_escalation(["src/omnimarket/models/x.py"], ADJACENCY)
        is EnumFullSuiteReason.SHARED_MODULE
    )
    assert (
        legacy_module_escalation(["src/omnimarket/github_api.py"], ADJACENCY)
        is EnumFullSuiteReason.UNMAPPED_MODULE
    )
    assert (
        legacy_module_escalation(
            [
                f"src/omnimarket/{m}/x.py"
                for m in ADJACENCY.adjacency
                if m not in ADJACENCY.shared_modules
            ][: ADJACENCY.thresholds.modules_changed_for_full_suite],
            ADJACENCY,
        )
        is EnumFullSuiteReason.THRESHOLD_MODULES
    )
    assert (
        legacy_module_escalation(["src/omnimarket/nodes/node_a/x.py"], ADJACENCY)
        is None
    )


def test_file_level_closure_stage_gate_on_the_real_tree() -> None:
    changed = ["src/omnimarket/models/__init__.py"]
    below = compute_selection(
        changed, ADJACENCY_PATH, "feature", stage=2, repo_root=REPO_ROOT
    )
    assert below.full_suite_reason is EnumFullSuiteReason.SHARED_MODULE
    at_stage = compute_selection(
        changed, ADJACENCY_PATH, "feature", stage=3, repo_root=REPO_ROOT
    )
    assert at_stage.full_suite_reason is not EnumFullSuiteReason.SHARED_MODULE
    default = compute_selection(changed, ADJACENCY_PATH, "feature", repo_root=REPO_ROOT)
    assert default == below, "the shipped stage (0) keeps today's selection"


def test_file_level_closure_unresolved_source_escalates(tmp_path: Path) -> None:
    _tree(tmp_path)
    _write(tmp_path, "src/omnimarket/_stray_dir/x.py", "X = 1\n")
    stray = "src/omnimarket/_stray_dir/x.py"
    # the resolver returns none first: the directory has no __init__.py
    assert resolve_source_module(tmp_path, stray) is None
    result = _close(tmp_path, stray)
    assert result.escalation is EnumClosureEscalation.UNRESOLVED_SOURCE
    assert result.unresolved_files == (stray,)
    # control: with the package marker the same file resolves and nothing escalates
    _write(tmp_path, "src/omnimarket/_stray_dir/__init__.py")
    assert resolve_source_module(tmp_path, stray) == "omnimarket._stray_dir.x"
    assert _close(tmp_path, stray).escalation is None


def test_file_level_closure_unresolved_source_beside_a_resolved_one_still_escalates(
    tmp_path: Path,
) -> None:
    _tree(tmp_path)
    _write(tmp_path, "src/omnimarket/models/data.json", "{}\n")
    result = _close(
        tmp_path, "src/omnimarket/models/model_m.py", "src/omnimarket/models/data.json"
    )
    assert result.escalation is EnumClosureEscalation.UNRESOLVED_SOURCE
    assert result.unresolved_files == ("src/omnimarket/models/data.json",)


def test_file_level_closure_unresolved_source_is_a_full_suite_reason_in_compute_selection(
    tmp_path: Path,
) -> None:
    changed = ["src/omnimarket/_stray_dir_not_in_tree/x.py"]
    selection = compute_selection(
        changed, ADJACENCY_PATH, "feature", stage=3, repo_root=REPO_ROOT
    )
    assert selection.is_full_suite
    assert selection.full_suite_reason is EnumFullSuiteReason.UNRESOLVED_SOURCE


def test_file_level_closure_deleted_model_selects_the_surviving_importer(
    tmp_path: Path,
) -> None:
    _tree(tmp_path)
    _write(
        tmp_path,
        "tests/models/test_old.py",
        "from omnimarket.models.model_gone import Gone\n\n\ndef test_old() -> None:\n    assert Gone\n",
    )
    result = _close(tmp_path, "src/omnimarket/models/model_gone.py")
    assert result.escalation is None
    assert "tests/models/test_old.py" in result.selected_paths
    # control: a test that never named the deleted model is not pulled in by the seed
    assert "tests/models/test_n.py" not in result.selected_paths


def test_file_level_closure_renamed_model_selects_the_test_that_keeps_the_old_name(
    tmp_path: Path,
) -> None:
    _tree(tmp_path)
    # model_old was renamed to model_new; a source module and a test still say the old name
    _write(tmp_path, "src/omnimarket/models/model_new.py", "class New: ...\n")
    _write(
        tmp_path,
        "src/omnimarket/models/model_user.py",
        "from . import model_old\n\n\ndef use() -> object:\n    return model_old\n",
    )
    _write(
        tmp_path,
        "tests/models/test_user.py",
        "from omnimarket.models.model_user import use\n\n\ndef test_user() -> None:\n    assert use\n",
    )
    _write(
        tmp_path,
        "tests/models/test_old_name.py",
        "from omnimarket.models import model_old\n\n\ndef test_old() -> None:\n    assert model_old\n",
    )
    result = _close(
        tmp_path,
        "src/omnimarket/models/model_old.py",
        "src/omnimarket/models/model_new.py",
    )
    assert result.escalation is None
    assert "tests/models/test_old_name.py" in result.selected_paths
    # a dependent reached only through the relative import of the old name
    assert "tests/models/test_user.py" in result.selected_paths


def test_file_level_closure_added_import_of_a_changed_model_selects_the_test(
    tmp_path: Path,
) -> None:
    _tree(tmp_path)
    before = _close(tmp_path, "src/omnimarket/models/model_m.py")
    assert "tests/test_bystander.py" not in before.selected_paths
    _write(
        tmp_path,
        "tests/test_bystander.py",
        "from omnimarket.models.model_m import M\n\n\ndef test_b() -> None:\n    assert M\n",
    )
    after = _close(tmp_path, "src/omnimarket/models/model_m.py")
    assert "tests/test_bystander.py" in after.selected_paths


def _nested_conftest(root: Path) -> None:
    _write(
        root,
        "tests/models/conftest.py",
        "import pytest\n\nfrom omnimarket.models.model_n import N\n\n\n"
        "@pytest.fixture(autouse=True)\ndef _n() -> type[N]:\n    return N\n",
    )
    _write(
        root,
        "tests/models/test_plain.py",
        "def test_plain() -> None: ...\n",
    )


def test_file_level_closure_nested_conftest_change_selects_its_directory_not_the_tree(
    tmp_path: Path,
) -> None:
    _tree(tmp_path)
    _nested_conftest(tmp_path)
    result = _close(tmp_path, "tests/models/conftest.py")
    assert result.escalation is None
    assert "tests/models/" in result.selected_paths
    assert "tests/" not in result.selected_paths
    assert "tests/nodes/node_a/" not in result.selected_paths


def test_file_level_closure_module_a_nested_conftest_imports_selects_its_directory(
    tmp_path: Path,
) -> None:
    _tree(tmp_path)
    _nested_conftest(tmp_path)
    result = _close(tmp_path, "src/omnimarket/models/model_n.py")
    assert "tests/models/" in result.selected_paths
    assert "tests/" not in result.selected_paths
    assert "tests/nodes/node_a/" not in result.selected_paths
    # the plain test has no import of the model; the nested conftest still reaches it
    assert result.collection_gate is False


def _root_conftest(root: Path) -> None:
    _write(
        root,
        "tests/conftest.py",
        "import pytest\n\nfrom omnimarket.models.model_n import N\n\n\n"
        "@pytest.fixture(autouse=True)\ndef _n() -> type[N]:\n    return N\n",
    )


def test_file_level_closure_root_conftest_edge_cut_runs_the_gate_not_the_tree(
    tmp_path: Path,
) -> None:
    _tree(tmp_path)
    _root_conftest(tmp_path)
    result = _close(
        tmp_path,
        "src/omnimarket/models/model_n.py",
        edge=EnumRootConftestEdge.CUT,
    )
    assert "tests/" not in result.selected_paths
    assert "tests/models/test_n.py" in result.selected_paths
    assert result.collection_gate is True


def test_file_level_closure_root_conftest_edge_kept_selects_the_tree(
    tmp_path: Path,
) -> None:
    _tree(tmp_path)
    _root_conftest(tmp_path)
    result = _close(
        tmp_path,
        "src/omnimarket/models/model_n.py",
        edge=EnumRootConftestEdge.KEEP,
    )
    assert "tests/" in result.selected_paths
    assert result.collection_gate is False


@pytest.mark.parametrize("edge", list(EnumRootConftestEdge))
def test_file_level_closure_a_change_to_the_root_conftest_itself_keeps_the_tree(
    tmp_path: Path, edge: EnumRootConftestEdge
) -> None:
    _tree(tmp_path)
    _root_conftest(tmp_path)
    result = _close(tmp_path, "tests/conftest.py", edge=edge)
    assert "tests/" in result.selected_paths


def test_file_level_closure_a_closure_that_misses_the_root_conftest_runs_no_gate(
    tmp_path: Path,
) -> None:
    _tree(tmp_path)
    _root_conftest(tmp_path)
    result = _close(tmp_path, "src/omnimarket/models/model_m.py")
    assert result.collection_gate is False


def test_file_level_closure_data_only_diff_selects_the_readers_when_the_switch_is_on(
    tmp_path: Path,
) -> None:
    _tree(tmp_path)
    _write(tmp_path, "contracts/OMN-1.yaml", "a: 1\n")
    _write(
        tmp_path,
        "tests/test_reads_contract.py",
        'from pathlib import Path\n\nTEXT = Path("contracts/OMN-1.yaml").read_text()\n',
    )
    on = ADJACENCY.model_copy(update={"data_only_selection_enabled": True})
    request = ModelFileLevelClosureRequest(
        changed_files=("contracts/OMN-1.yaml",),
        adjacency=on,
        repo_root=str(tmp_path),
        root_conftest_edge=EnumRootConftestEdge.CUT,
    )
    result = HandlerFileLevelClosure().handle(request)
    assert result.data_only is True
    assert result.selected_paths == ("tests/test_reads_contract.py",)
    off = HandlerFileLevelClosure().handle(
        request.model_copy(update={"adjacency": ADJACENCY})
    )
    assert off.selected_paths == ()
    assert off.data_only is False


def test_file_level_closure_requests_and_results_are_frozen_and_forbid_extras() -> None:
    with pytest.raises(ValidationError):
        ModelFileLevelClosureRequest.model_validate(
            {
                "changed_files": [],
                "adjacency": ADJACENCY.model_dump(),
                "repo_root": "/x",
                "root_conftest_edge": "cut",
                "surprise": 1,
            }
        )
    result = ModelFileLevelClosureResult(selected_paths=("tests/a/",))
    with pytest.raises(ValidationError):
        result.selected_paths = ()  # type: ignore[misc]
    with pytest.raises(ValidationError):
        ModelFileLevelClosureResult(
            selected_paths=("tests/a/",),
            escalation=EnumClosureEscalation.UNRESOLVED_SOURCE,
        )


# ---- golden and error chain of the operation over the in-memory bus ----


def _contract() -> dict[str, Any]:
    document: dict[str, Any] = yaml.safe_load(
        files("omnimarket.nodes.node_test_selection_selector_compute")
        .joinpath("contract.yaml")
        .read_text()
    )
    return document


def test_file_level_closure_operation_is_routed_by_the_contract() -> None:
    routes = {h["operation"]: h for h in _contract()["handler_routing"]["handlers"]}
    route = routes["resolve_file_level_closure"]
    assert route["input_model"].endswith(
        "model_file_level_closure.ModelFileLevelClosureRequest"
    )
    assert route["output_model"].endswith(
        "model_file_level_closure.ModelFileLevelClosureResult"
    )
    module = importlib.import_module(route["handler"]["module"])
    assert getattr(module, route["handler"]["name"]) is HandlerFileLevelClosure


async def _chain(root: Path, payload: bytes) -> list[tuple[str, bytes]]:
    contract = _contract()
    request_topic = contract["runtime_dispatch"]["command_topic"]
    terminal_topic = contract["terminal_event"]
    failed_topic = contract["runtime_dispatch"]["terminal_events"]["failure"]
    bus = EventBusInmemory()
    seen: list[tuple[str, bytes]] = []
    done: asyncio.Future[None] = asyncio.get_running_loop().create_future()

    async def handle(message: Any) -> None:
        try:
            request = ModelFileLevelClosureRequest.model_validate_json(message.value)
        except ValidationError as exc:
            await bus.publish(failed_topic, key=None, value=type(exc).__name__.encode())
            return
        await bus.publish(
            terminal_topic,
            key=None,
            value=HandlerFileLevelClosure().handle(request).model_dump_json().encode(),
        )

    def collector(topic: str) -> Any:
        async def collect(message: Any) -> None:
            seen.append((topic, message.value))
            if not done.done():
                done.set_result(None)

        return collect

    await bus.start()
    try:
        await bus.subscribe(request_topic, on_message=handle, group_id="flc")
        for topic in (terminal_topic, failed_topic):
            await bus.subscribe(
                topic, on_message=collector(topic), group_id=f"f-{topic}"
            )
        await bus.publish(request_topic, key=None, value=payload)
        await asyncio.wait_for(done, timeout=5)
    finally:
        await bus.close()
    return seen


@pytest.mark.asyncio
async def test_file_level_closure_golden_chain_ends_in_the_terminal_event(
    tmp_path: Path,
) -> None:
    _tree(tmp_path)
    payload = ModelFileLevelClosureRequest(
        changed_files=("src/omnimarket/models/model_m.py",),
        adjacency=ADJACENCY,
        repo_root=str(tmp_path),
        root_conftest_edge=EnumRootConftestEdge.CUT,
    ).model_dump_json()
    seen = await _chain(tmp_path, payload.encode())
    assert [t for t, _ in seen] == [_contract()["terminal_event"]]
    result = ModelFileLevelClosureResult.model_validate_json(seen[0][1])
    assert "tests/models/test_m.py" in result.selected_paths


@pytest.mark.asyncio
async def test_file_level_closure_error_chain_unresolved_source_is_an_escalation_event(
    tmp_path: Path,
) -> None:
    _tree(tmp_path)
    _write(tmp_path, "src/omnimarket/_stray_dir/x.py", "X = 1\n")
    payload = ModelFileLevelClosureRequest(
        changed_files=("src/omnimarket/_stray_dir/x.py",),
        adjacency=ADJACENCY,
        repo_root=str(tmp_path),
        root_conftest_edge=EnumRootConftestEdge.CUT,
    ).model_dump_json()
    seen = await _chain(tmp_path, payload.encode())
    assert [t for t, _ in seen] == [_contract()["terminal_event"]]
    result = ModelFileLevelClosureResult.model_validate_json(seen[0][1])
    assert result.escalation is EnumClosureEscalation.UNRESOLVED_SOURCE


@pytest.mark.asyncio
async def test_file_level_closure_error_chain_unparseable_request_is_a_failure_event(
    tmp_path: Path,
) -> None:
    seen = await _chain(tmp_path, b'{"changed_files": "not-a-list"}')
    assert [t for t, _ in seen] == [
        _contract()["runtime_dispatch"]["terminal_events"]["failure"]
    ]
