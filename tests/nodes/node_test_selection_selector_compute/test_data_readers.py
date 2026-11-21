# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A changed fixture or data file selects the tests that read it, directly, through a helper or through a source file.

For a changed non-Python file under a data-reader root (``tests/``, ``contracts/``, a node directory) the readers
are the test files whose text spells its path, its file name or a directory between ``tests/fixtures/`` and the
file, the tests that enumerate the directory, the reverse import closure of any helper among those readers, the
tests that import a ``src/`` file naming the file, and the readers ``data_reader_overrides`` declares. A file
with no reader selects the always set only: a path built at run time with no literal part is the nightly's to
catch, not this lookup's. The rule runs only where ``data_file_readers_enabled`` is true; with it off a changed
fixture keeps the full-suite escalation.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from omnimarket.nodes.node_test_selection_selector_compute.models.model_test_selection_record import (
    EnumFullSuiteReason,
    ModelTestSelection,
)
from scripts.ci.detect_test_paths import REPO_ROOT, compute_selection
from scripts.ci.test_selection_loader import load_adjacency_map

pytestmark = pytest.mark.unit

ADJACENCY = REPO_ROOT / "scripts" / "ci" / "test_selection_adjacency.yaml"
ALWAYS = {"tests/gates/", "tests/ci/test_binding.py"}

# The data files the four fixture-only merges of the last 60 on dev changed (first-parent history of dev
# at aef9f49df): each merge's non-Python files under tests/, as `git show --name-only` lists them.
FOUR_FIXTURE_MERGES: dict[str, list[str]] = {
    "7819ffecd0": ["tests/fixtures/scope_bundle_held_not_array.json"],
    "006a153ee0": ["tests/fixtures/routing_sources/inventory.yaml"],
    "bd745d07d1": ["tests/fixtures/routing_sources/inventory.yaml"],
    "b9e96a63b8": [
        "tests/fixtures/deploy_receipt_omn19998/completed_failed_owned.json",
        "tests/fixtures/deploy_receipt_omn19998/completed_not_owned.json",
        "tests/fixtures/deploy_receipt_omn19998/completed_owned.json",
        "tests/fixtures/deploy_receipt_omn19998/rejected_route_unconfirmed.json",
        "tests/nodes/node_branch_deploy_command_effect/agent_wire.json",
    ],
}

# The two fixture merges that also changed the lock. 79f31e93f5's lock diff swaps a dependency (certifi);
# ae69d56977's is the package's own version line, the exception that belongs to the pyproject rules.
TWO_LOCK_MERGES: dict[str, list[str]] = {
    "79f31e93f5": [
        "pyproject.toml",
        "tests/fixtures/openrouter_models_2026_11_20.json",
        "tests/fixtures/receipts/cheap_frontier_smokes_2026_11_20.json",
        "uv.lock",
    ],
    "ae69d56977": [
        "pyproject.toml",
        "tests/fixtures/routing_sources/inventory.yaml",
        "uv.lock",
    ],
}


def _adjacency(tmp_path: Path, **settings: object) -> Path:
    """The real adjacency file with the rule on and ``settings`` changed, for one test."""
    raw = yaml.safe_load(ADJACENCY.read_text(encoding="utf-8"))
    raw["data_file_readers_enabled"] = True
    raw.update(settings)
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


def _covers(selected: tuple[str, ...] | list[str], path: str) -> bool:
    return any(path == s or (s.endswith("/") and path.startswith(s)) for s in selected)


# --- the real tree -----------------------------------------------------------------------------


def test_data_readers_shipped_adjacency_declares_the_rule_and_keeps_it_off() -> None:
    config = load_adjacency_map(ADJACENCY)
    assert config.data_reader_roots == ["tests/", "contracts/", "src/omnimarket/nodes/"]
    assert config.data_reader_fixture_roots == ["tests/fixtures/"]
    # The rule changes what a pull request runs; the change that turns it on is its own.
    assert config.data_file_readers_enabled is False
    # Fixtures are no longer unconditional test infrastructure; the rest still is.
    assert "tests/fixtures/" not in config.test_infrastructure_paths
    assert {
        "tests/conftest.py",
        "pytest.ini",
        ".pre-commit-config.yaml",
        "uv.lock",
    } <= set(config.test_infrastructure_paths)
    # While the rule is off a fixture keeps the escalation it had.
    assert config.data_reader_gated_infrastructure_paths == ["tests/fixtures/"]


@pytest.mark.parametrize(("sha", "changed"), FOUR_FIXTURE_MERGES.items())
def test_data_readers_four_fixture_merges_narrow_and_select_every_reader(
    sha: str, changed: list[str], tmp_path: Path
) -> None:
    selection = _selection(changed, _adjacency(tmp_path))
    assert not selection.is_full_suite, sha
    assert "tests/" not in selection.selected_paths
    assert set(selection.selected_paths) >= ALWAYS_REAL
    # The falsifier of the ticket: every test file whose text names a changed file is selected.
    for data_file in changed:
        name = data_file.rsplit("/", 1)[-1]
        hits = [
            p.relative_to(REPO_ROOT).as_posix()
            for p in (REPO_ROOT / "tests").rglob("test_*.py")
            if name in p.read_text(encoding="utf-8", errors="ignore")
        ]
        missing = [p for p in hits if not _covers(selection.selected_paths, p)]
        assert not missing, (sha, data_file, missing)


ALWAYS_REAL = {
    "tests/gates/",
    "tests/test_no_cross_node_reach_in.py",
    "tests/ci/test_repo_evidence_caller_workflow.py",
}


def test_data_readers_routing_sources_fixture_selects_the_tests_that_spell_the_directory(
    tmp_path: Path,
) -> None:
    selection = _selection(
        ["tests/fixtures/routing_sources/inventory.yaml"], _adjacency(tmp_path)
    )
    assert "tests/test_routing_sources_omn20601.py" in selection.selected_paths


@pytest.mark.parametrize(("sha", "changed"), TWO_LOCK_MERGES.items())
def test_data_readers_a_lock_change_beside_a_fixture_stays_a_full_suite(
    sha: str, changed: list[str], tmp_path: Path
) -> None:
    # The lock version-line exception for ae69d56977 belongs to the pyproject rules; until they land, both
    # merges keep the escalation, and the narrowing of the second is asserted again after they merge.
    selection = _selection(changed, _adjacency(tmp_path))
    assert selection.is_full_suite, sha
    assert selection.full_suite_reason is EnumFullSuiteReason.TEST_INFRASTRUCTURE


def test_data_readers_catalog_yaml_selects_the_tests_that_name_it(
    tmp_path: Path,
) -> None:
    changed = [
        "tests/nodes/node_workflow_gateway_effect/fixtures/workflow_catalog.yaml"
    ]
    selection = _selection(changed, _adjacency(tmp_path))
    assert not selection.is_full_suite
    hits = [
        p.relative_to(REPO_ROOT).as_posix()
        for p in (REPO_ROOT / "tests").rglob("test_*.py")
        if "workflow_catalog.yaml" in p.read_text(encoding="utf-8", errors="ignore")
    ]
    assert hits
    assert all(_covers(selection.selected_paths, p) for p in hits)


# --- a fixture tree: what counts as a reader ---------------------------------------------------

_CONFIG = {
    "schema_version": 1,
    "shared_modules": [],
    "thresholds": {"modules_changed_for_full_suite": 6},
    "test_infrastructure_paths": [
        "uv.lock",
        "pyproject.toml",
        "pytest.ini",
        ".pre-commit-config.yaml",
        "tests/conftest.py",
    ],
    "adjacency": {"nodes": {"reverse_deps": []}, "handlers": {"reverse_deps": []}},
    "node_scoped_modules": ["nodes"],
    "always_selected_paths": sorted(ALWAYS),
    "data_file_readers_enabled": True,
    "data_reader_roots": ["tests/", "contracts/", "src/omnimarket/nodes/"],
    "data_reader_fixture_roots": ["tests/fixtures/"],
    "data_reader_gated_infrastructure_paths": ["tests/fixtures/"],
}

_TREE = {
    "tests/__init__.py": "",
    "tests/gates/__init__.py": "",
    "tests/gates/test_gate.py": "def test_gate() -> None: ...\n",
    "tests/ci/__init__.py": "",
    "tests/ci/test_binding.py": "def test_binding() -> None: ...\n",
    "tests/fixtures/__init__.py": "",
    "tests/fixtures/routing_sources/inventory.yaml": "a: 1\n",
    "tests/fixtures/routing_sources/other.yaml": "b: 2\n",
    "tests/fixtures/catalog_nodes/catalog.yaml": "nodes: []\n",
    "tests/fixtures/topology/topology.yaml": "edges: []\n",
    "tests/helpers/__init__.py": "",
    "tests/helpers/fixture_loader.py": (
        "def load(root):\n"
        '    return (root / "fixtures" / "routing_sources" / "inventory.yaml").read_text()\n'
    ),
    "tests/test_via_helper_a.py": "from tests.helpers.fixture_loader import load\n",
    "tests/test_via_helper_b.py": "from tests.helpers import fixture_loader\n",
    "tests/test_spells_path.py": 'P = "tests/fixtures/routing_sources/inventory.yaml"\n',
    "tests/test_spells_name.py": 'N = "inventory.yaml"\n',
    "tests/test_spells_directory.py": 'D = "routing_sources"\n',
    "tests/test_iterdir_dynamic.py": (
        "from pathlib import Path\n"
        'SUBDIR = "routing_sources"\n'
        'CASES = sorted((Path(__file__).parent / "fixtures" / SUBDIR).iterdir())\n'
    ),
    "tests/test_reads_through_source.py": (
        "from omnimarket.handlers import inventory_reader\n"
    ),
    "tests/test_reads_through_facade.py": (
        "from omnimarket.handlers.inventory_facade import FACADE\n"
    ),
    "src/omnimarket/handlers/inventory_reader.py": 'NAME = "inventory.yaml"\n',
    "src/omnimarket/handlers/inventory_facade.py": (
        "from omnimarket.handlers import inventory_reader\nFACADE = inventory_reader\n"
    ),
    "src/omnimarket/nodes/node_alpha/__init__.py": "",
    "src/omnimarket/nodes/node_alpha/schema.json": "{}\n",
    "tests/nodes/node_alpha/__init__.py": "",
    "tests/nodes/node_alpha/test_alpha.py": "def test_alpha() -> None: ...\n",
    "tests/test_alpha_schema.py": 'S = "node_alpha/schema.json"\n',
    "tests/test_catalog.py": 'C = "catalog_nodes"\n',
    "tests/test_topology.py": 'T = "fixtures/topology/topology.yaml"\n',
    "tests/test_other_directory.py": 'O = "other_directory"\n',
    "tests/test_unrelated.py": "def test_unrelated() -> None: ...\n",
}

INVENTORY = "tests/fixtures/routing_sources/inventory.yaml"
INVENTORY_READERS = {
    "tests/test_via_helper_a.py",
    "tests/test_via_helper_b.py",
    "tests/test_spells_path.py",
    "tests/test_spells_name.py",
    "tests/test_spells_directory.py",
    "tests/test_iterdir_dynamic.py",
    "tests/test_reads_through_source.py",
    "tests/test_reads_through_facade.py",
}
NOT_INVENTORY_READERS = {
    "tests/test_other_directory.py",
    "tests/test_unrelated.py",
    "tests/test_alpha_schema.py",
    "tests/test_catalog.py",
    "tests/test_topology.py",
}


def _fixture(
    root: Path,
    *,
    tree: dict[str, str] | None = None,
    settings: dict[str, object] | None = None,
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
    adjacency.write_text(yaml.safe_dump({**_CONFIG, **(settings or {})}))
    return adjacency


def _selected(
    root: Path,
    changed: list[str],
    *,
    tree: dict[str, str] | None = None,
    settings: dict[str, object] | None = None,
) -> set[str]:
    adjacency = _fixture(root, tree=tree, settings=settings)
    selection = _selection(changed, adjacency, repo_root=root)
    assert not selection.is_full_suite, selection.full_suite_reason
    return set(selection.selected_paths)


def test_data_readers_fixture_selects_every_reader_and_the_always_set(
    tmp_path: Path,
) -> None:
    selected = _selected(tmp_path, [INVENTORY])
    assert selected == ALWAYS | INVENTORY_READERS
    # Controls: tests that name another fixture directory, another file or nothing are not readers.
    assert not selected & NOT_INVENTORY_READERS


def test_data_readers_test_building_the_path_from_a_directory_named_in_a_string_is_found(
    tmp_path: Path,
) -> None:
    selected = _selected(tmp_path, [INVENTORY])
    assert "tests/test_iterdir_dynamic.py" in selected
    # Control: with the directory rule's roots removed the same test is no reader, because its text
    # spells neither the path nor the file name and its enumeration is through a variable.
    without = _selected(
        tmp_path / "no_rule", [INVENTORY], settings={"data_reader_fixture_roots": []}
    )
    assert "tests/test_iterdir_dynamic.py" not in without
    assert "tests/test_spells_directory.py" not in without


def test_data_readers_test_enumerating_the_directory_with_iterdir_is_found(
    tmp_path: Path,
) -> None:
    changed = "tests/data/topology/edges.yaml"
    tree = {
        changed: "edges: []\n",
        "tests/test_enumerates.py": (
            "from pathlib import Path\n"
            'FILES = list((Path(__file__).parent / "data" / "topology").iterdir())\n'
        ),
    }
    selected = _selected(tmp_path, [changed], tree=tree)
    assert "tests/test_enumerates.py" in selected
    # Control: a file in a directory it does not enumerate is not read by it.
    other = _selected(tmp_path / "other", [INVENTORY], tree=tree)
    assert "tests/test_enumerates.py" not in other


def test_data_readers_fixture_read_only_through_a_shared_helper_selects_both_importers(
    tmp_path: Path,
) -> None:
    selected = _selected(tmp_path, [INVENTORY])
    assert {"tests/test_via_helper_a.py", "tests/test_via_helper_b.py"} <= selected
    # Control: remove the helper's reference and neither importer is a reader.
    without = _selected(
        tmp_path / "no_helper",
        [INVENTORY],
        tree={
            "tests/helpers/fixture_loader.py": "def load(root):\n    return ''\n",
        },
    )
    assert not without & {"tests/test_via_helper_a.py", "tests/test_via_helper_b.py"}


def test_data_readers_fixture_read_by_a_source_file_selects_its_importers(
    tmp_path: Path,
) -> None:
    selected = _selected(tmp_path, [INVENTORY])
    # One test imports the source file that names the fixture; the other reaches it through a facade.
    assert {
        "tests/test_reads_through_source.py",
        "tests/test_reads_through_facade.py",
    } <= selected
    # Control: a source file that does not name the fixture makes both importers non-readers.
    without = _selected(
        tmp_path / "no_source",
        [INVENTORY],
        tree={"src/omnimarket/handlers/inventory_reader.py": "NAME = 'x'\n"},
    )
    assert not without & {
        "tests/test_reads_through_source.py",
        "tests/test_reads_through_facade.py",
    }


def test_data_readers_data_file_beside_a_node_selects_the_node_tests_and_its_readers(
    tmp_path: Path,
) -> None:
    selected = _selected(tmp_path, ["src/omnimarket/nodes/node_alpha/schema.json"])
    assert "tests/test_alpha_schema.py" in selected
    assert "tests/nodes/node_alpha/" in selected
    assert not selected & {"tests/test_unrelated.py", "tests/test_catalog.py"}


def test_data_readers_catalog_and_topology_yaml_select_their_readers(
    tmp_path: Path,
) -> None:
    catalog = _selected(tmp_path / "c", ["tests/fixtures/catalog_nodes/catalog.yaml"])
    assert catalog == ALWAYS | {"tests/test_catalog.py"}
    topology = _selected(tmp_path / "t", ["tests/fixtures/topology/topology.yaml"])
    assert topology == ALWAYS | {"tests/test_topology.py"}


def test_data_readers_two_data_files_select_the_readers_of_both(
    tmp_path: Path,
) -> None:
    selected = _selected(
        tmp_path,
        [
            "tests/fixtures/catalog_nodes/catalog.yaml",
            "tests/fixtures/topology/topology.yaml",
        ],
    )
    assert selected == ALWAYS | {"tests/test_catalog.py", "tests/test_topology.py"}


def test_data_readers_a_deleted_file_looks_up_its_old_path(tmp_path: Path) -> None:
    gone = "tests/fixtures/retired_dir/retired.json"
    tree = {"tests/test_names_retired.py": 'R = "retired.json"\n'}
    adjacency = _fixture(tmp_path, tree=tree)
    assert not (tmp_path / gone).exists()
    selection = _selection([gone], adjacency, repo_root=tmp_path)
    assert not selection.is_full_suite
    assert set(selection.selected_paths) == ALWAYS | {"tests/test_names_retired.py"}


def test_data_readers_a_rename_looks_up_the_old_and_the_new_path(
    tmp_path: Path,
) -> None:
    old = "tests/fixtures/topology/old_name.yaml"
    new = "tests/fixtures/topology/new_name.yaml"
    tree = {
        new: "edges: []\n",
        "tests/test_names_old.py": 'O = "old_name.yaml"\n',
        "tests/test_names_new.py": 'N = "new_name.yaml"\n',
    }
    selected = _selected(tmp_path, [old, new], tree=tree)
    assert {"tests/test_names_old.py", "tests/test_names_new.py"} <= selected


def test_data_readers_a_file_with_no_reader_selects_the_always_set_only(
    tmp_path: Path,
) -> None:
    adjacency = _fixture(
        tmp_path, tree={"tests/fixtures/orphans/nobody_reads_this.json": "{}\n"}
    )
    selection = _selection(
        ["tests/fixtures/orphans/nobody_reads_this.json"], adjacency, tmp_path
    )
    assert not selection.is_full_suite
    assert set(selection.selected_paths) == ALWAYS


def test_data_readers_overrides_name_a_reader_the_text_cannot_show(
    tmp_path: Path,
) -> None:
    selected = _selected(
        tmp_path,
        ["tests/fixtures/orphans/nobody_reads_this.json"],
        tree={"tests/fixtures/orphans/nobody_reads_this.json": "{}\n"},
        settings={
            "data_reader_overrides": {
                "tests/fixtures/orphans/*": ["tests/test_unrelated.py"]
            }
        },
    )
    assert selected == ALWAYS | {"tests/test_unrelated.py"}


@pytest.mark.parametrize(
    "stays",
    [
        "uv.lock",
        "pyproject.toml",
        "pytest.ini",
        ".pre-commit-config.yaml",
        "tests/conftest.py",
    ],
)
def test_data_readers_test_infrastructure_beside_a_fixture_stays_a_full_suite(
    stays: str, tmp_path: Path
) -> None:
    adjacency = _fixture(tmp_path)
    selection = _selection([INVENTORY, stays], adjacency, tmp_path)
    assert selection.is_full_suite
    assert selection.full_suite_reason is EnumFullSuiteReason.TEST_INFRASTRUCTURE


def test_data_readers_a_python_file_under_the_fixtures_tree_stays_a_full_suite(
    tmp_path: Path,
) -> None:
    # The rule names non-Python data files only; a fixture module a test imports keeps the escalation.
    adjacency = _fixture(tmp_path)
    selection = _selection(["tests/fixtures/__init__.py"], adjacency, tmp_path)
    assert selection.is_full_suite
    assert selection.full_suite_reason is EnumFullSuiteReason.TEST_INFRASTRUCTURE


def test_data_readers_a_data_file_beside_an_unattributable_file_takes_the_full_suite(
    tmp_path: Path,
) -> None:
    adjacency = _fixture(tmp_path)
    selection = _selection([INVENTORY, "Dockerfile"], adjacency, tmp_path)
    assert selection.is_full_suite
    assert selection.full_suite_reason is EnumFullSuiteReason.NO_NARROWABLE_SELECTION


def test_data_readers_a_fixture_keeps_the_full_suite_while_the_rule_is_off(
    tmp_path: Path,
) -> None:
    adjacency = _fixture(tmp_path, settings={"data_file_readers_enabled": False})
    selection = _selection([INVENTORY], adjacency, tmp_path)
    assert selection.is_full_suite
    assert selection.full_suite_reason is EnumFullSuiteReason.TEST_INFRASTRUCTURE


def test_data_readers_a_contract_file_selects_its_readers_when_the_rule_is_on(
    tmp_path: Path,
) -> None:
    selected = _selected(
        tmp_path,
        ["contracts/sample.yaml"],
        tree={"tests/test_spells_contract.py": 'C = "contracts/sample.yaml"\n'},
    )
    assert selected == ALWAYS | {"tests/test_spells_contract.py"}
