# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A change of only contract files or a changelog selects the always set and the tests that read them.

The rule replaces the ``no_narrowable_selection`` full-suite escalation for a diff whose files lie
outside ``src/`` and ``tests/`` and match ``data_only_globs``. It selects the always set, the tests whose
text spells the file or enumerates its directory (through an imported helper too) and the readers the
adjacency file declares in ``data_reader_overrides``. Any other unmatched file keeps the full escalation.
The rule runs only where ``data_only_selection_enabled`` is true.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from omnimarket.handlers.adapter_test_selection_policy_file import load_policy
from omnimarket.nodes.node_test_selection_selector_compute.adapters.adjacency_file import (
    load_adjacency_map,
)
from omnimarket.nodes.node_test_selection_selector_compute.models.model_test_selection_record import (
    EnumFullSuiteReason,
    ModelTestSelection,
)
from scripts.ci.detect_test_paths import REPO_ROOT, compute_selection

pytestmark = pytest.mark.unit

ADJACENCY = REPO_ROOT / "scripts" / "ci" / "test_selection_adjacency.yaml"
CONTRACT_BINDING_TEST = "tests/ci/test_repo_evidence_caller_workflow.py"

# The five merges of the last 60 on dev whose diff held no code (first-parent history of dev at
# 26126773a): four changed only a ticket contract, one only the changelog.
FIVE_DATA_ONLY_MERGES: dict[str, list[str]] = json.loads(
    (Path(__file__).parent / "five_data_only_merges.json").read_text(encoding="utf-8")
)

# The changelog's name comes from the recorded merge, not from this module's text: spelled here, it made
# this test a reader of the changelog by the selector's own text index, and a changelog merge ran it.
CHANGELOG = list(FIVE_DATA_ONLY_MERGES.values())[-1][0]

# AC1 of OMN-26270: the most recorded test seconds a data-only merge may select.
AC1_BOUND_SECONDS = 51.0

ALWAYS_SET = (
    "tests/gates/",
    "tests/test_no_cross_node_reach_in.py",
    CONTRACT_BINDING_TEST,
)


def _adjacency(
    tmp_path: Path,
    *,
    enabled: bool = True,
    always: tuple[str, ...] | None = None,
    overrides: dict[str, list[str]] | None = None,
) -> Path:
    """The real adjacency file with the rule's settings changed, for one test."""
    raw = yaml.safe_load(ADJACENCY.read_text(encoding="utf-8"))
    raw["data_only_selection_enabled"] = enabled
    if always is not None:
        raw["always_selected_paths"] = list(always)
    if overrides is not None:
        raw["data_reader_overrides"] = overrides
    path = tmp_path / "adjacency.yaml"
    path.write_text(yaml.safe_dump(raw))
    return path


def _selection(
    changed: list[str], adjacency: Path, repo_root: Path | None = None
) -> ModelTestSelection:
    return compute_selection(
        changed_files=changed,
        adjacency_path=adjacency,
        ref_name="feature",
        repo_root=repo_root,
    )


def _seconds(selection: ModelTestSelection) -> float:
    # The record the shard planner reads: the shared list and the one-file entries beside it
    # (config/test_file_durations.d), which hold every test file added since the shared list was cut.
    durations: dict[str, float] = json.loads(
        (REPO_ROOT / "config" / "test_file_durations.json").read_text()
    )
    for entry in sorted(
        (REPO_ROOT / "config" / "test_file_durations.d").glob("*.json")
    ):
        durations.update(json.loads(entry.read_text(encoding="utf-8")))
    return float(
        sum(
            seconds
            for test_file, seconds in durations.items()
            if any(
                test_file == path or (path.endswith("/") and test_file.startswith(path))
                for path in selection.selected_paths
            )
        )
    )


# --- the real tree -----------------------------------------------------------------------------


def test_data_only_shipped_adjacency_declares_the_rule_and_turns_it_on() -> None:
    config = load_adjacency_map(ADJACENCY)
    assert config.data_only_globs == ["contracts/OMN-*.yaml", "*.md"]
    # Stage 1 is entered (policy selection_stage 1, receipt stage_1.yaml), so the rule is live.
    assert load_policy().selection_stage >= 1
    assert config.data_only_selection_enabled is True


# The files whose time decides AC1: the always set's tests that read every contract, and the readers a
# contract change selects. Each parses the contract tree, over two thousand ticket contracts and more
# than a thousand node contracts, and the pure-Python YAML loader was nearly all of their seconds.
LIBYAML_FILES = (
    "scripts/ci/check_contract_publish_parity.py",
    "tests/gates/test_consumer_group_contract_declaration_strip.py",
    "tests/gates/test_terminal_event_topic_not_consumed.py",
    "tests/ci/test_repo_evidence_caller_workflow.py",
    "tests/ci/test_contract_dod_evidence_ids_unique_omn21733.py",
    "tests/ci/test_contract_evidence_selector_names_file_omn17427.py",
)


@pytest.mark.parametrize("relative", LIBYAML_FILES)
def test_data_only_contract_readers_parse_with_the_libyaml_loader(
    relative: str,
) -> None:
    text = (REPO_ROOT / relative).read_text(encoding="utf-8")
    assert "CSafeLoader" in text, relative
    assert "yaml.safe_load(path.read_text" not in text, relative


@pytest.mark.parametrize(("sha", "changed"), FIVE_DATA_ONLY_MERGES.items())
def test_data_only_five_merges_are_narrowed_by_the_shipped_adjacency(
    sha: str, changed: list[str]
) -> None:
    """The shipped adjacency file, unmodified, narrows each of the five merges."""
    selection = _selection(changed, ADJACENCY)
    assert not selection.is_full_suite, sha
    assert selection.full_suite_reason is None
    assert set(ALWAYS_SET) <= set(selection.selected_paths)


@pytest.mark.parametrize(("sha", "changed"), FIVE_DATA_ONLY_MERGES.items())
def test_data_only_five_merges_are_narrowed_to_the_always_set_and_readers(
    sha: str, changed: list[str], tmp_path: Path
) -> None:
    selection = _selection(changed, _adjacency(tmp_path))
    assert not selection.is_full_suite, sha
    assert selection.full_suite_reason is None
    assert set(ALWAYS_SET) <= set(selection.selected_paths)
    assert "tests/" not in selection.selected_paths
    assert selection.split_count <= 5
    # AC1: each of the five merges selects 51 recorded seconds or less. The always set and the
    # readers a contract change must run (the tests that enumerate contracts/) fit that bound only
    # while the gates parse the contract tree quickly (OMN-26270).
    assert _seconds(selection) <= AC1_BOUND_SECONDS


@pytest.mark.parametrize(("sha", "changed"), FIVE_DATA_ONLY_MERGES.items())
def test_data_only_five_merges_stay_full_while_the_rule_is_off(
    sha: str, changed: list[str], tmp_path: Path
) -> None:
    selection = _selection(changed, _adjacency(tmp_path, enabled=False))
    assert selection.is_full_suite, sha
    assert selection.full_suite_reason is EnumFullSuiteReason.NO_NARROWABLE_SELECTION


def test_data_only_unmatched_non_code_file_still_escalates_to_full(
    tmp_path: Path,
) -> None:
    selection = _selection(["Dockerfile"], _adjacency(tmp_path))
    assert selection.is_full_suite
    assert selection.full_suite_reason is EnumFullSuiteReason.NO_NARROWABLE_SELECTION


def test_data_only_one_unmatched_file_among_data_files_escalates_to_full(
    tmp_path: Path,
) -> None:
    selection = _selection(
        [next(iter(FIVE_DATA_ONLY_MERGES.values()))[0], "Dockerfile"],
        _adjacency(tmp_path),
    )
    assert selection.is_full_suite
    assert selection.full_suite_reason is EnumFullSuiteReason.NO_NARROWABLE_SELECTION


def test_data_only_contract_binding_test_is_in_the_shipped_always_set() -> None:
    assert CONTRACT_BINDING_TEST in load_adjacency_map(ADJACENCY).always_selected_paths


# --- a fixture tree: what counts as a reader ---------------------------------------------------

_CONFIG = {
    "schema_version": 1,
    "shared_modules": [],
    "thresholds": {"modules_changed_for_full_suite": 6},
    "test_infrastructure_paths": [],
    "adjacency": {"nodes": {"reverse_deps": []}},
    "always_selected_paths": ["tests/gates/", "tests/ci/test_binding.py"],
    "data_only_selection_enabled": True,
    "data_only_globs": ["contracts/OMN-*.yaml", "*.md"],
}

# The fixture tree sits in a data file: spelled in this module's text, the glob calls it holds made this
# test a reader of every contract by the selector's own text index, and a contract merge then ran it.
_TREE: dict[str, str] = json.loads(
    (Path(__file__).parent / "data_only_fixture_tree.json").read_text(encoding="utf-8")
)


def _fixture(
    root: Path,
    *,
    overrides: dict[str, list[str]] | None = None,
    always: list[str] | None = None,
    tree: dict[str, str] | None = None,
) -> Path:
    files = {**_TREE, **(tree or {})}
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    (root / "config").mkdir(exist_ok=True)
    (root / "config" / "test_file_durations.json").write_text(
        json.dumps({rel: 1.0 for rel in files if rel.startswith("tests/")})
    )
    adjacency = root / "adjacency.yaml"
    adjacency.write_text(
        yaml.safe_dump(
            {
                **_CONFIG,
                "data_reader_overrides": overrides or {},
                **({"always_selected_paths": always} if always is not None else {}),
            }
        )
    )
    return adjacency


def _fixture_selected(
    root: Path,
    changed: list[str],
    *,
    overrides: dict[str, list[str]] | None = None,
    always: list[str] | None = None,
    tree: dict[str, str] | None = None,
) -> set[str]:
    adjacency = _fixture(root, overrides=overrides, always=always, tree=tree)
    return set(_selection(changed, adjacency, repo_root=root).selected_paths)


def test_data_only_test_enumerating_the_directory_with_glob_is_selected(
    tmp_path: Path,
) -> None:
    selected = _fixture_selected(tmp_path, ["contracts/OMN-9.yaml"])
    assert "tests/test_enumerates.py" in selected
    assert "tests/gates/" in selected
    # Controls: a test that globs another directory, one that names nothing, and a changelog reader
    # are not readers of a contract.
    assert not selected & {
        "tests/test_globs_docs.py",
        "tests/test_unrelated.py",
        "tests/test_reads_changelog.py",
    }


def test_data_only_test_spelling_the_changed_file_is_selected(tmp_path: Path) -> None:
    selected = _fixture_selected(tmp_path, ["contracts/OMN-1.yaml"])
    assert "tests/test_spells_the_file.py" in selected
    # A contract the test does not spell is not read by it.
    other = _fixture_selected(tmp_path, ["contracts/OMN-2.yaml"])
    assert "tests/test_spells_the_file.py" not in other


def test_data_only_test_whose_only_reference_is_in_an_imported_helper_is_selected(
    tmp_path: Path,
) -> None:
    selected = _fixture_selected(tmp_path, ["contracts/OMN-9.yaml"])
    assert "tests/test_via_helper.py" in selected
    # Control: the importer is found through the helper closure, so a test that does not import the
    # helper is not selected.
    without = _fixture_selected(
        tmp_path / "no_import",
        ["contracts/OMN-9.yaml"],
        tree={"tests/test_via_helper.py": "X = 1\n"},
    )
    assert "tests/test_via_helper.py" not in without


def test_data_only_reader_named_only_in_the_overrides_is_selected(
    tmp_path: Path,
) -> None:
    overrides = {"contracts/OMN-*.yaml": ["tests/test_hidden.py"]}
    selected = _fixture_selected(
        tmp_path, ["contracts/OMN-9.yaml"], overrides=overrides
    )
    assert "tests/test_hidden.py" in selected
    # Control: the override is the only thing that names it, and it does not apply to a changelog.
    assert "tests/test_hidden.py" not in _fixture_selected(
        tmp_path, [CHANGELOG], overrides=overrides
    )
    assert "tests/test_hidden.py" not in _fixture_selected(
        tmp_path, ["contracts/OMN-9.yaml"]
    )


def test_data_only_contract_binding_test_runs_on_every_narrowed_selection(
    tmp_path: Path,
) -> None:
    for changed in (["contracts/OMN-9.yaml"], [CHANGELOG]):
        assert "tests/ci/test_binding.py" in _fixture_selected(
            tmp_path / changed[0].replace("/", "_"), changed
        )
    # Control: drop the binding test from the always set and the same assertion fails, so the
    # selection carries it through the always set and not by a reader hit.
    without = _fixture_selected(
        tmp_path / "dropped", ["contracts/OMN-9.yaml"], always=["tests/gates/"]
    )
    assert "tests/ci/test_binding.py" not in without


def test_data_only_data_file_beside_an_unmatched_file_takes_the_full_suite(
    tmp_path: Path,
) -> None:
    adjacency = _fixture(tmp_path)
    selection = _selection(["contracts/OMN-9.yaml", "Dockerfile"], adjacency, tmp_path)
    assert selection.is_full_suite
    assert selection.full_suite_reason is EnumFullSuiteReason.NO_NARROWABLE_SELECTION


def test_data_only_changelog_selects_its_reader_and_the_always_set(
    tmp_path: Path,
) -> None:
    selected = _fixture_selected(tmp_path, [CHANGELOG])
    assert selected == {
        "tests/gates/",
        "tests/ci/test_binding.py",
        "tests/test_reads_changelog.py",
    }


def test_data_only_markdown_inside_src_or_tests_is_not_a_data_only_file(
    tmp_path: Path,
) -> None:
    adjacency = _fixture(tmp_path)
    under_src = _selection(["src/omnimarket/README.md"], adjacency, tmp_path)
    assert under_src.full_suite_reason is EnumFullSuiteReason.UNMAPPED_MODULE
    under_tests = _selection(["tests/README.md"], adjacency, tmp_path)
    assert under_tests.full_suite_reason is EnumFullSuiteReason.NO_NARROWABLE_SELECTION
