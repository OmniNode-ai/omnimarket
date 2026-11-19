# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The customer package omninode-onex (OMN-25070, AC2 and AC3).

omninode-onex is a metadata-only distribution: it carries no code, declares the
two customer launchers ``onex`` and ``omni-agent`` with omnibase_core's CLI as
their target, and pins each distribution it installs with ``==``. A customer
runs ``uv tool install omninode-onex``, which links only the named package's
launchers, so exactly ``onex`` and ``omni-agent`` land on PATH.

Failure modes each test catches: a launcher missing or an extra one added; a
launcher whose target differs from the one omnibase_core declares; a range pin
that lets a customer resolve an unreviewed version; a wheel that copies another
distribution's code; built wheel metadata that drifts from pyproject.toml.
"""

from __future__ import annotations

import importlib.metadata
import shutil
import subprocess
import tomllib
import zipfile
from pathlib import Path

import pytest
from packaging.requirements import Requirement
from packaging.version import Version

REPO_ROOT = Path(__file__).resolve().parents[2]
PACKAGE_DIR = REPO_ROOT / "packages" / "omninode-onex"
PYPROJECT = PACKAGE_DIR / "pyproject.toml"
CUSTOMER_LAUNCHERS = {"onex", "omni-agent"}
PINNED_DISTRIBUTIONS = {"omnibase-core", "omnibase-infra", "omnimarket"}


def _project() -> dict[str, object]:
    assert PYPROJECT.is_file(), f"{PYPROJECT} is missing"
    with PYPROJECT.open("rb") as stream:
        data = tomllib.load(stream)
    project = data["project"]
    assert isinstance(project, dict)
    return project


def _core_launcher_targets() -> dict[str, str]:
    entry_points = importlib.metadata.distribution("omnibase-core").entry_points
    return {
        ep.name: ep.value
        for ep in entry_points
        if ep.group == "console_scripts" and ep.name in CUSTOMER_LAUNCHERS
    }


@pytest.mark.unit
def test_package_declares_exactly_the_customer_launchers() -> None:
    project = _project()
    assert project["name"] == "omninode-onex"
    scripts = project.get("scripts")
    assert isinstance(scripts, dict)
    assert set(scripts) == CUSTOMER_LAUNCHERS


@pytest.mark.unit
def test_launchers_target_the_cli_omnibase_core_declares() -> None:
    scripts = _project()["scripts"]
    assert isinstance(scripts, dict)
    core_targets = _core_launcher_targets()
    assert set(core_targets) == CUSTOMER_LAUNCHERS, core_targets
    for name in CUSTOMER_LAUNCHERS:
        assert scripts[name] == core_targets[name], name


@pytest.mark.unit
def test_every_dependency_is_pinned_exactly() -> None:
    dependencies = _project()["dependencies"]
    assert isinstance(dependencies, list)
    requirements = [Requirement(str(item)) for item in dependencies]
    assert {r.name for r in requirements} == PINNED_DISTRIBUTIONS
    for requirement in requirements:
        specifiers = list(requirement.specifier)
        assert len(specifiers) == 1, requirement
        assert specifiers[0].operator == "==", requirement
        assert not requirement.extras, requirement
        assert requirement.marker is None, requirement


@pytest.mark.unit
def test_omnimarket_pin_is_not_newer_than_this_repository() -> None:
    # omninode-onex releases on its own tag, so its omnimarket pin may trail
    # this repository's version; it must never name an unreleased, newer one.
    with (REPO_ROOT / "pyproject.toml").open("rb") as stream:
        own_version = Version(tomllib.load(stream)["project"]["version"])
    dependencies = _project()["dependencies"]
    assert isinstance(dependencies, list)
    pins = {
        Requirement(str(item)).name: Requirement(str(item)) for item in dependencies
    }
    pinned = Version(next(iter(pins["omnimarket"].specifier)).version)
    assert pinned <= own_version, (pinned, own_version)


@pytest.mark.unit
def test_built_wheel_carries_no_code_and_matches_pyproject(tmp_path: Path) -> None:
    uv = shutil.which("uv")
    if uv is None:
        pytest.skip("uv is not on PATH")
    project = _project()
    built = subprocess.run(
        [uv, "build", "--wheel", "--out-dir", str(tmp_path), str(PACKAGE_DIR)],
        capture_output=True,
        text=True,
        check=False,
        timeout=600,
    )
    assert built.returncode == 0, built.stderr
    wheels = list(tmp_path.glob("omninode_onex-*.whl"))
    assert len(wheels) == 1, wheels
    with zipfile.ZipFile(wheels[0]) as wheel:
        names = wheel.namelist()
        dist_info = f"omninode_onex-{project['version']}.dist-info/"
        assert names
        assert all(name.startswith(dist_info) for name in names), names
        entry_points = wheel.read(dist_info + "entry_points.txt").decode()
        metadata = wheel.read(dist_info + "METADATA").decode()
    declared = {
        line.split("=", 1)[0].strip()
        for line in entry_points.splitlines()
        if "=" in line
    }
    assert declared == CUSTOMER_LAUNCHERS, entry_points
    requires = sorted(
        line.split(":", 1)[1].strip()
        for line in metadata.splitlines()
        if line.startswith("Requires-Dist:")
    )
    dependencies = project["dependencies"]
    assert isinstance(dependencies, list)
    assert requires == sorted(str(item) for item in dependencies)


@pytest.mark.unit
def test_release_workflow_has_its_own_tag_and_checks_the_installer_pin() -> None:
    import fnmatch

    import yaml

    workflows = REPO_ROOT / ".github" / "workflows"
    onex = yaml.safe_load((workflows / "release-omninode-onex.yml").read_text())
    market = yaml.safe_load((workflows / "release.yml").read_text())
    onex_tags = onex[True]["push"]["tags"]
    market_tags = market[True]["push"]["tags"]
    version = _project()["version"]
    onex_tag = f"omninode-onex-v{version}"
    assert any(fnmatch.fnmatch(onex_tag, p) for p in onex_tags)
    assert not any(fnmatch.fnmatch(onex_tag, p) for p in market_tags)
    assert not any(fnmatch.fnmatch("v0.4.321", p) for p in onex_tags)
    steps = "\n".join(
        str(step.get("run", "")) for step in onex["jobs"]["release"]["steps"]
    )
    assert "packages/omninode-onex/pyproject.toml" in steps
    assert "packages/omninode-onex/installer.json" in steps
    assert "node_onex_installer_render_compute.runtime_onex_installer_render" in steps
    assert "verify_pypi_pin_resolvability.py dist/" in steps
