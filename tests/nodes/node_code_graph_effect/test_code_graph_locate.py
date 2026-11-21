# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The locate question: where a node, its contract, overlay and workflows live (OMN-26202, AC1).

The graph answers from the stored version, never from a file read, and Memgraph and the file store
answer alike from the same version.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from omnimarket.models.model_code_graph_lookup import (
    EnumCodeGraphQuestion,
    ModelCodeGraphLookupCommand,
    ModelCodeGraphLookupResult,
)
from omnimarket.nodes.node_code_graph_effect import runtime_code_graph
from omnimarket.nodes.node_code_graph_effect.adapters.memgraph_version_store import (
    MemgraphVersionStore,
)
from omnimarket.nodes.node_code_graph_effect.adapters.version_stores import (
    InMemoryCommitClock,
    InMemoryVersionStore,
)
from omnimarket.nodes.node_code_graph_effect.handlers.code_graph_lookup import (
    CodeGraphLookup,
)
from omnimarket.nodes.node_code_graph_effect.handlers.handler_code_graph_requested import (
    HandlerCodeGraphLookupRequested,
)
from tests.nodes.node_code_graph_effect.code_graph_support import EXAMPLE, make_clone
from tests.nodes.node_code_graph_effect.lookup_support import (
    BASE,
    CORRELATION,
    REPO,
    version,
)
from tests.nodes.node_code_graph_effect.memgraph_support import FakeBoltDriver

pytestmark = pytest.mark.unit

ALPHA = "src/example_core/nodes/node_alpha_effect"
BETA = "src/example_core/nodes/node_beta_effect"
WORKFLOW = ".github/workflows/alpha.yml"
SHARED_WORKFLOW = ".github/workflows/both.yml"
UNRELATED_WORKFLOW = ".github/workflows/lint.yml"

FILES: dict[str, str] = {
    **BASE,
    f"{ALPHA}/__init__.py": "",
    f"{ALPHA}/metadata.yaml": "name: node_alpha_effect\n",
    f"{ALPHA}/overlay.example.yaml": "synthetic: true\n",
    f"{BETA}/__init__.py": "",
    f"{BETA}/overlay.yaml": "synthetic: true\n",
    WORKFLOW: "jobs:\n  run:\n    steps:\n      - run: python -m example_core.nodes.node_alpha_effect check\n",
    SHARED_WORKFLOW: (
        "jobs:\n  run:\n    steps:\n"
        "      - run: python -m example_core.nodes.node_alpha_effect check\n"
        "      - run: python src/example_core/nodes/node_beta_effect/runtime.py\n"
    ),
    UNRELATED_WORKFLOW: "jobs:\n  run:\n    steps:\n      - run: ruff check\n",
}


def _locate(target: str) -> ModelCodeGraphLookupCommand:
    return ModelCodeGraphLookupCommand(
        correlation_id=CORRELATION,
        question=EnumCodeGraphQuestion.LOCATE,
        repo=REPO,
        target=target,
    )


def _file_store() -> InMemoryVersionStore:
    store = InMemoryVersionStore(retention_versions=3)
    store.put(version("c1", FILES))
    return store


def _memgraph() -> MemgraphVersionStore:
    ticks = iter(range(1, 1000))
    store = MemgraphVersionStore(
        FakeBoltDriver(), retention_versions=3, clock_ns=lambda: next(ticks)
    )
    store.put(version("c1", FILES))
    return store


def _paths(result: ModelCodeGraphLookupResult) -> list[str]:
    return sorted(match.path for match in result.matches)


def _answer(command: ModelCodeGraphLookupCommand) -> ModelCodeGraphLookupResult:
    return CodeGraphLookup(_file_store(), InMemoryCommitClock({})).handle(command)


def test_locate_names_the_node_directory_contract_metadata_overlay_and_workflow() -> (
    None
):
    result = _answer(_locate("node_alpha_effect"))
    assert result.status == "found"
    assert result.answered_from == "c1"
    assert _paths(result) == sorted(
        [
            ALPHA,
            f"{ALPHA}/contract.yaml",
            f"{ALPHA}/metadata.yaml",
            f"{ALPHA}/overlay.example.yaml",
            WORKFLOW,
            SHARED_WORKFLOW,
        ]
    )
    kinds = {match.path: match.detail for match in result.matches}
    assert kinds[ALPHA] == "directory"
    assert kinds[f"{ALPHA}/contract.yaml"] == "contract"
    assert kinds[f"{ALPHA}/metadata.yaml"] == "metadata"
    assert kinds[f"{ALPHA}/overlay.example.yaml"] == "overlay"
    assert kinds[WORKFLOW] == "workflow"


def test_locate_names_the_overlay_yaml_of_a_node_that_has_one() -> None:
    result = _answer(_locate("node_beta_effect"))
    assert f"{BETA}/overlay.yaml" in _paths(result)
    assert SHARED_WORKFLOW in _paths(result)
    assert WORKFLOW not in _paths(result)


def test_locate_by_contract_path_answers_the_same_node() -> None:
    by_name = _answer(_locate("node_alpha_effect"))
    by_contract = _answer(_locate(f"{ALPHA}/contract.yaml"))
    assert _paths(by_contract) == _paths(by_name)


def test_locate_by_workflow_path_answers_the_nodes_it_names() -> None:
    result = _answer(_locate(SHARED_WORKFLOW))
    assert f"{ALPHA}/contract.yaml" in _paths(result)
    assert f"{BETA}/contract.yaml" in _paths(result)
    assert UNRELATED_WORKFLOW not in _paths(result)


def test_a_workflow_that_names_no_node_locates_nothing() -> None:
    result = _answer(_locate(UNRELATED_WORKFLOW))
    assert result.status == "not-found"
    assert result.matches == ()


def test_an_unknown_node_is_a_clean_not_found() -> None:
    result = _answer(_locate("node_no_such_thing"))
    assert result.status == "not-found"
    assert result.reason == "the version holds no answer to the question"


def test_a_node_without_workflow_or_overlay_still_has_its_directory_and_contract() -> (
    None
):
    result = _answer(_locate("node_gamma_compute"))
    assert _paths(result) == [
        "src/example_core/nodes/node_gamma_compute",
        "src/example_core/nodes/node_gamma_compute/contract.yaml",
    ]


def test_locate_without_a_target_is_refused() -> None:
    with pytest.raises(ValidationError):
        ModelCodeGraphLookupCommand(
            correlation_id=CORRELATION,
            question=EnumCodeGraphQuestion.LOCATE,
            repo=REPO,
        )


def test_a_version_built_before_node_files_were_stored_says_the_question_is_not_indexed() -> (
    None
):
    old = version("c1", FILES).model_copy(update={"node_files": ()})
    store = InMemoryVersionStore(retention_versions=3)
    store.put(old)
    result = CodeGraphLookup(store, InMemoryCommitClock({})).handle(
        _locate("node_alpha_effect")
    )
    assert result.status == "not-found"
    assert (result.reason or "").startswith("question-not-indexed")


@pytest.mark.parametrize(
    "target",
    [
        "node_alpha_effect",
        "node_beta_effect",
        f"{ALPHA}/contract.yaml",
        SHARED_WORKFLOW,
        UNRELATED_WORKFLOW,
        "node_no_such_thing",
    ],
)
def test_memgraph_and_the_file_store_answer_locate_alike(target: str) -> None:
    command = _locate(target)
    from_files = CodeGraphLookup(_file_store(), InMemoryCommitClock({})).handle(command)
    memgraph = _memgraph()
    from_memgraph = CodeGraphLookup(memgraph, memgraph).handle(command)
    assert from_memgraph == from_files


def test_the_memgraph_write_stores_node_files_as_cypher_nodes() -> None:
    driver = FakeBoltDriver()
    ticks = iter(range(1, 1000))
    store = MemgraphVersionStore(
        driver, retention_versions=3, clock_ns=lambda: next(ticks)
    )
    store.put(version("c1", FILES))
    stored = {
        (row["node"], row["kind"], row["path"])
        for row in driver.nodes["CgNodeFile"].values()
    }
    assert ("node_alpha_effect", "contract", f"{ALPHA}/contract.yaml") in stored
    assert ("node_alpha_effect", "workflow", WORKFLOW) in stored


def test_the_bound_handler_locates_a_node_from_a_git_clone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clones = tmp_path / "clones"
    make_clone(clones, "example_core", FILES)
    document = yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))
    document["graph_backend"] = "git"
    target = tmp_path / "overlays" / "node_code_graph_effect" / "overlay.yaml"
    target.parent.mkdir(parents=True)
    target.write_text(yaml.safe_dump(document), encoding="utf-8")
    monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", str(tmp_path / "overlays"))
    monkeypatch.setenv("EXAMPLE_REPOS_ROOT", str(clones))
    runtime_code_graph._VERSIONS.clear()
    result = HandlerCodeGraphLookupRequested().handle(_locate("node_alpha_effect"))
    assert result.status == "found"
    assert f"{ALPHA}/metadata.yaml" in _paths(result)
    assert WORKFLOW in _paths(result)
