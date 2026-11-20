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

from scripts.ci.detect_test_paths import REPO_ROOT, compute_selection
from scripts.ci.test_selection_loader import load_adjacency_map
from scripts.ci.test_selection_models import EnumFullSuiteReason, ModelTestSelection

pytestmark = pytest.mark.unit

ADJACENCY = REPO_ROOT / "scripts" / "ci" / "test_selection_adjacency.yaml"
CONTRACT_BINDING_TEST = "tests/ci/test_repo_evidence_caller_workflow.py"

# The five merges of the last 60 on dev whose diff held no code (first-parent history of dev at
# 26126773a): four changed only a ticket contract, one only the changelog.
FIVE_DATA_ONLY_MERGES: dict[str, list[str]] = {
    "aaf1652192e12c15e83d693b079985aec4e41e93": ["contracts/OMN-21787.yaml"],
    "b21fd5d9b53e5954a396a887171e2f5db89aedc7": ["contracts/OMN-21735.yaml"],
    "d66f9e22f8941ef3b53425126c6f4b6d0fb58b97": ["contracts/OMN-21694.yaml"],
    "cf6fd12a57d3f86aac6b402809062dfc946b090a": ["contracts/OMN-21632.yaml"],
    "5bf30a3f5258f3e797bd86d05a5847dca9efcc82": ["CHANGELOG.md"],
}

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
    durations = json.loads(
        (REPO_ROOT / "config" / "test_file_durations.json").read_text()
    )
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


def test_data_only_shipped_adjacency_declares_the_rule_and_keeps_it_off() -> None:
    config = load_adjacency_map(ADJACENCY)
    assert config.data_only_globs == ["contracts/OMN-*.yaml", "*.md"]
    # The rule changes what a pull request runs; it is switched on by its own change, not by this one.
    assert config.data_only_selection_enabled is False


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
    # The always set alone is 50.8 recorded seconds; the readers a contract change must run
    # (nine tests that enumerate contracts/) bring the total to the ceiling pinned here.
    assert _seconds(selection) <= 90.0


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
        ["contracts/OMN-21787.yaml", "Dockerfile"], _adjacency(tmp_path)
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

_TREE = {
    "tests/__init__.py": "",
    "tests/gates/__init__.py": "",
    "tests/gates/test_gate.py": "def test_gate() -> None: ...\n",
    "tests/ci/__init__.py": "",
    "tests/ci/test_binding.py": "def test_binding() -> None: ...\n",
    "tests/helpers/__init__.py": "",
    "tests/helpers/contracts_helper.py": (
        "def all_contracts(root):\n"
        '    return sorted((root / "contracts").glob("OMN-*.yaml"))\n'
    ),
    "tests/test_enumerates.py": (
        "from pathlib import Path\n"
        'FILES = list((Path(__file__).parents[1] / "contracts").glob("OMN-*.yaml"))\n'
    ),
    "tests/test_via_helper.py": (
        "from tests.helpers.contracts_helper import all_contracts\n"
    ),
    "tests/test_spells_the_file.py": 'PATH = "contracts/OMN-1.yaml"\n',
    "tests/test_reads_changelog.py": 'TEXT = open("CHANGELOG.md").read()\n',
    "tests/test_hidden.py": "def test_hidden() -> None: ...\n",
    "tests/test_globs_docs.py": (
        "from pathlib import Path\n"
        'FILES = list((Path(__file__).parents[1] / "docs").glob("*.md"))\n'
    ),
    "tests/test_unrelated.py": "def test_unrelated() -> None: ...\n",
}


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
        tmp_path, ["CHANGELOG.md"], overrides=overrides
    )
    assert "tests/test_hidden.py" not in _fixture_selected(
        tmp_path, ["contracts/OMN-9.yaml"]
    )


def test_data_only_contract_binding_test_runs_on_every_narrowed_selection(
    tmp_path: Path,
) -> None:
    for changed in (["contracts/OMN-9.yaml"], ["CHANGELOG.md"]):
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
    selected = _fixture_selected(tmp_path, ["CHANGELOG.md"])
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
