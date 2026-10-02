# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Acceptance tests for deterministic delegation splitting."""

from pathlib import Path

import pytest
import yaml

from omnimarket.models.model_delegation_split_recombine import (
    ModelDelegationSplitRequest,
    ModelDelegationSplitSuccess,
    ModelNotDecomposable,
)
from omnimarket.nodes.node_delegation_split_compute import HandlerDelegationSplit

pytestmark = pytest.mark.unit

FILE_A = "diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n"
HUNK_A = "@@ -1 +1 @@\n-old_a\n+new_a\n"
HUNK_B = "@@ -9 +9 @@\n-old_b\n+new_b\n"
FILE_B = "diff --git a/b.py b/b.py\n--- a/b.py\n+++ b/b.py\n"
ROOT = Path(__file__).resolve().parents[3]


@pytest.mark.parametrize("task_class", ["summarization", "document"])
def test_summarization_split_by_section(task_class: str) -> None:
    sections = ("# First\nalpha\n", "# Second\nbeta\n", "# Third\ngamma\n")
    request = ModelDelegationSplitRequest(
        task_id="task",
        task_class=task_class,
        size_band="large",
        source="".join(sections),
    )
    result = HandlerDelegationSplit().handle(request)
    assert isinstance(result, ModelDelegationSplitSuccess)
    assert result.task_id == request.task_id
    assert tuple(unit.source for unit in result.units) == sections
    assert tuple(unit.position for unit in result.units) == (0, 1, 2)
    assert all(unit.size_band == "medium" for unit in result.units)
    assert all(unit.task_class == task_class for unit in result.units)
    assert len({unit.unit_id for unit in result.units}) == 3
    assert HandlerDelegationSplit().handle(request) == result


@pytest.mark.parametrize("task_class", ["code_review", "review"])
def test_review_split_per_file(task_class: str) -> None:
    handler = HandlerDelegationSplit()
    first = FILE_A + HUNK_A + HUNK_B
    second = FILE_B + HUNK_A
    result = handler.handle(
        ModelDelegationSplitRequest(
            task_id="review",
            task_class=task_class,
            size_band="large",
            source=first + second,
        )
    )
    assert isinstance(result, ModelDelegationSplitSuccess)
    assert tuple(unit.source for unit in result.units) == (first, second)
    assert all(unit.size_band == "medium" for unit in result.units)
    hunks = handler.handle(
        ModelDelegationSplitRequest(
            task_id="review", task_class=task_class, size_band="medium", source=first
        )
    )
    assert isinstance(hunks, ModelDelegationSplitSuccess)
    assert tuple(unit.source for unit in hunks.units) == (
        FILE_A + HUNK_A,
        FILE_A + HUNK_B,
    )
    assert all(unit.size_band == "small" for unit in hunks.units)


@pytest.mark.parametrize("task_class", ["planning", "reasoning", "code_generation"])
def test_not_decomposable_typed(task_class: str) -> None:
    contract = yaml.safe_load(
        (
            ROOT / "src/omnimarket/nodes/node_delegation_split_compute/contract.yaml"
        ).read_text()
    )
    assert contract["split_rules"][task_class] == "not_decomposable"
    result = HandlerDelegationSplit().handle(
        ModelDelegationSplitRequest(
            task_id="opaque",
            task_class=task_class,
            size_band="large",
            source="# A\na\n# B\nb\n",
        )
    )
    assert isinstance(result, ModelNotDecomposable)
    assert result.status == "not_decomposable"
    assert result.reason == "task_class_not_decomposable"
    assert result.task_class == task_class


def test_migration_split_by_listed_unit() -> None:
    result = HandlerDelegationSplit().handle(
        ModelDelegationSplitRequest(
            task_id="migration",
            task_class="migration",
            size_band="large",
            migration_units=("ALTER TABLE alpha;", "ALTER TABLE beta;"),
        )
    )
    assert isinstance(result, ModelDelegationSplitSuccess)
    assert tuple(unit.source for unit in result.units) == (
        "ALTER TABLE alpha;",
        "ALTER TABLE beta;",
    )
    assert all(unit.size_band == "medium" for unit in result.units)


@pytest.mark.parametrize(
    ("source", "band", "reason"),
    [
        ("plain text", "large", "no_split_boundary"),
        ("# A\na\n# B\nb\n", "small", "minimum_size_band"),
    ],
)
def test_unsplittable_source_is_typed(source: str, band: str, reason: str) -> None:
    result = HandlerDelegationSplit().handle(
        ModelDelegationSplitRequest(
            task_id="task",
            task_class="summarization",
            size_band=band,
            source=source,
        )
    )
    assert isinstance(result, ModelNotDecomposable)
    assert result.reason == reason


def test_section_headers_inside_fences_are_source_content() -> None:
    first = "intro\n# A\n```python\n# not a section\n```\n"
    result = HandlerDelegationSplit().handle(
        ModelDelegationSplitRequest(
            task_id="task",
            task_class="document",
            size_band="large",
            source=first + "# B\nend\n",
        )
    )
    assert isinstance(result, ModelDelegationSplitSuccess)
    assert "".join(unit.source for unit in result.units) == first + "# B\nend\n"
    assert len(result.units) == 3  # preamble and two real sections


def test_split_contract_rules_and_registration() -> None:
    import tomllib
    from importlib import import_module

    contract = yaml.safe_load(
        (
            ROOT / "src/omnimarket/nodes/node_delegation_split_compute/contract.yaml"
        ).read_text()
    )
    assert contract["split_rules"] == {
        "summarization": "by_source_section",
        "code_review": "per_file_then_per_hunk",
        "review": "per_file_then_per_hunk",
        "document": "per_section",
        "migration": "per_listed_unit",
        "planning": "not_decomposable",
        "reasoning": "not_decomposable",
        "code_generation": "not_decomposable",
    }
    entries = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"][
        "entry-points"
    ]["onex.nodes"]
    terminal_topics = {
        "node_delegation_split_compute": "onex.evt.omnimarket.delegation-split-completed.v1",
        "node_delegation_recombine_compute": "onex.evt.omnimarket.delegation-recombine-completed.v1",
    }
    for name in ("node_delegation_split_compute", "node_delegation_recombine_compute"):
        node_dir = ROOT / "src/omnimarket/nodes" / name
        config = yaml.safe_load((node_dir / "contract.yaml").read_text())
        assert import_module(entries[name])
        assert config["node_type"] == "compute"
        assert config["terminal_event"] == terminal_topics[name]
        assert config["event_bus"]["publish_topics"] == [config["terminal_event"]]
        assert config["descriptor"]["purity"] == "pure"
        assert (
            yaml.safe_load((node_dir / "metadata.yaml").read_text())["capabilities"][
                "requires_network"
            ]
            is False
        )
