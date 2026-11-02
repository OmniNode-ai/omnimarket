# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""The metadata group names stay in the announcer's metadata reader (OMN-21597, A1 AC8, AC10).

Both ratchets are tests over text and carry a planted-violation fixture, so a
ratchet that can no longer fail is itself a failing test.
"""

import os
import re
import subprocess
from pathlib import Path

import pytest

import omnimarket.nodes.node_package_announce_effect as node_announce_package

GROUP_NAME = re.compile(r"onex\.nodes|onex\.domain_plugins")
CONCRETE_BUS = re.compile(r"EventBusInmemory|RecordingBus|BlackholeBus|EventBusKafka")
READER = "package_metadata_reader.py"
HELPERS = {
    "helper_announce_buses.py",
}


def scrubbed_git_env() -> dict[str, str]:
    """The environment without the git location variables a hook exports."""
    return {
        key: value
        for key, value in os.environ.items()
        if key
        not in {
            "GIT_DIR",
            "GIT_WORK_TREE",
            "GIT_INDEX_FILE",
            "GIT_COMMON_DIR",
            "GIT_PREFIX",
        }
    }


def files_naming_a_group(directory: Path) -> set[str]:
    """Names of code and contract files under ``directory`` that mention a metadata group.

    ``metadata.yaml`` is the package's own metadata, which has to name the group it
    registers under, so it is not a catalog-side or container-side module and is not scanned.
    """
    return {
        str(path.relative_to(directory))
        for path in directory.rglob("*")
        if path.is_file()
        and (path.suffix == ".py" or path.name == "contract.yaml")
        and GROUP_NAME.search(path.read_text(encoding="utf-8"))
    }


def files_naming_a_concrete_bus(directory: Path, *, allowed: set[str]) -> set[str]:
    """Names of test modules, other than the allowed helpers, that name a bus class."""
    return {
        path.name
        for path in directory.glob("*.py")
        if path.name not in allowed
        and CONCRETE_BUS.search(path.read_text(encoding="utf-8"))
    }


@pytest.mark.unit
def test_group_names_appear_only_in_the_metadata_reader() -> None:
    directory = Path(node_announce_package.__file__).parent
    assert files_naming_a_group(directory) == {READER}, (
        "falsifier: a match outside the announcer's metadata reader"
    )


@pytest.mark.unit
def test_git_grep_agrees_with_the_ratchet() -> None:
    directory = Path(node_announce_package.__file__).parent
    result = subprocess.run(
        [
            "git",
            "grep",
            "--no-index",
            "-l",
            "-E",
            GROUP_NAME.pattern,
            "--",
            "*.py",
            "contract.yaml",
        ],
        capture_output=True,
        text=True,
        check=False,
        cwd=directory,
        env=scrubbed_git_env(),
    )
    assert result.returncode in {0, 1}, result.stderr
    assert {Path(line).name for line in result.stdout.splitlines()} == {READER}


@pytest.mark.unit
def test_planted_group_name_on_the_catalog_side_is_caught(tmp_path: Path) -> None:
    """Planted violation: a catalog-side module that names the group must be reported."""
    (tmp_path / READER).write_text('GROUP = "onex.nodes"\n', encoding="utf-8")
    (tmp_path / "service_planted_catalog.py").write_text(
        'import importlib.metadata\nentry_points(group="onex.domain_plugins")\n',
        encoding="utf-8",
    )
    assert files_naming_a_group(tmp_path) == {READER, "service_planted_catalog.py"}


@pytest.mark.unit
def test_no_test_module_names_a_concrete_bus_class() -> None:
    directory = Path(__file__).parent
    assert (
        files_naming_a_concrete_bus(directory, allowed=HELPERS | {Path(__file__).name})
        == set()
    ), "falsifier: a test that names a concrete bus class"


@pytest.mark.unit
def test_planted_concrete_bus_in_a_test_is_caught(tmp_path: Path) -> None:
    (tmp_path / "test_planted.py").write_text(
        "bus = EventBusInmemory()\n", encoding="utf-8"
    )
    assert files_naming_a_concrete_bus(tmp_path, allowed=set()) == {"test_planted.py"}
