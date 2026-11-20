# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The customer package omninode-onex (OMN-25070, AC2, AC3 and AC5).

omninode-onex is a metadata-only distribution: it carries no code, declares the
two customer launchers ``onex`` and ``omni-agent`` with omnibase_core's CLI as
their target, and pins each distribution it installs with ``==``. A customer
runs ``uv tool install omninode-onex``, which links only the named package's
launchers, so exactly ``onex`` and ``omni-agent`` land on PATH.

Failure modes each test catches: a launcher missing or an extra one added; a
launcher whose target differs from the one omnibase_core declares; a range pin
that lets a customer resolve an unreviewed version; a wheel that copies another
distribution's code; built wheel metadata that drifts from pyproject.toml.

AC5 (amended 2026-11-18): T1 removes no console script marked ``run_by_lane``.
omnibase_infra's ``config/validation/stack_shape.yaml`` lists every omnimarket
console script with a status; a ``run_by_lane`` one is commanded by a compose
carrier, so dropping it from omnimarket's ``[project.scripts]`` breaks a running
stack, and the customer package must not take it over. Failure modes those
tests catch: a run_by_lane script deleted from pyproject.toml; one whose target
was changed; one added to the customer package; the literal fallback list
drifting from the infra stack shape (the fallback is what hosted CI checks,
where no omnibase_infra checkout exists). Their negative tests run the same
checkers against a scratch copy of a pyproject.toml, so each is shown to fail
without touching the real file.
"""

from __future__ import annotations

import importlib.metadata
import os
import shutil
import subprocess
import tomllib
import zipfile
from pathlib import Path

import pytest
import yaml
from packaging.requirements import Requirement
from packaging.version import Version

REPO_ROOT = Path(__file__).resolve().parents[2]
PACKAGE_DIR = REPO_ROOT / "packages" / "omninode-onex"
PYPROJECT = PACKAGE_DIR / "pyproject.toml"
CUSTOMER_LAUNCHERS = {"onex", "omni-agent"}
# Every first-party distribution a customer install contains, so a later
# release of any of them cannot change what a customer gets.
PINNED_DISTRIBUTIONS = {
    "omnibase-core",
    "omnibase-infra",
    "omnibase-spi",
    "omnimarket",
    "omninode-memory",
}
FIRST_PARTY_PREFIXES = ("omnibase-", "omninode-")


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
def test_pins_cover_and_satisfy_omnimarkets_first_party_ranges() -> None:
    # A first-party dependency omnimarket declares but the customer package
    # leaves unpinned floats to whatever is newest on PyPI at install time.
    with (REPO_ROOT / "pyproject.toml").open("rb") as stream:
        declared = tomllib.load(stream)["project"]["dependencies"]
    ranges = {
        r.name: r
        for r in (Requirement(str(item)) for item in declared)
        if r.name.startswith(FIRST_PARTY_PREFIXES)
    }
    dependencies = _project()["dependencies"]
    assert isinstance(dependencies, list)
    pins = {
        r.name: Version(next(iter(r.specifier)).version)
        for r in (Requirement(str(item)) for item in dependencies)
    }
    unpinned = sorted(set(ranges) - set(pins))
    assert not unpinned, f"first-party dependencies left unpinned: {unpinned}"
    outside = {
        name: (str(pins[name]), str(r.specifier))
        for name, r in ranges.items()
        if not r.specifier.contains(pins[name], prereleases=True)
    }
    assert not outside, f"pins outside omnimarket's declared ranges: {outside}"


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


# AC5: the console scripts the infra stack runs stay declared.

MARKET_PYPROJECT = REPO_ROOT / "pyproject.toml"
STACK_SHAPE_RELATIVE = Path("config") / "validation" / "stack_shape.yaml"
RUN_BY_LANE = "run_by_lane"

# Mirrors omnibase_infra dev config/validation/stack_shape.yaml, console_scripts
# -> omnimarket -> omnimarket-projection-api (status: run_by_lane), read
# 2026-11-20. When a sibling checkout exists,
# test_run_by_lane_literal_matches_infra_stack_shape fails if this drifts.
RUN_BY_LANE_LITERAL: dict[str, str] = {
    "omnimarket-projection-api": "omnimarket.projection.api_server:main",
}


def _find_infra_stack_shape() -> Path | None:
    """Locate a sibling omnibase_infra checkout's stack_shape.yaml, or None."""
    roots: list[Path] = []
    workspace_root = os.environ.get("OMNI_HOME")
    if workspace_root:
        roots.append(Path(workspace_root))
    roots.extend(REPO_ROOT.resolve().parents)
    for root in roots:
        for candidate in (
            root / "omnibase_infra",
            root / "omnibase" / "repos" / "omnibase_infra",
        ):
            path = candidate / STACK_SHAPE_RELATIVE
            if path.is_file():
                return path
    return None


def _derive_run_by_lane(stack_shape: Path) -> dict[str, str]:
    data = yaml.safe_load(stack_shape.read_text(encoding="utf-8"))
    entries = data["console_scripts"]["omnimarket"]
    return {
        name: entry["target"]
        for name, entry in entries.items()
        if entry.get("status") == RUN_BY_LANE
    }


def _expected_run_by_lane() -> dict[str, str]:
    stack_shape = _find_infra_stack_shape()
    if stack_shape is not None:
        return _derive_run_by_lane(stack_shape)
    return dict(RUN_BY_LANE_LITERAL)


def _scripts(pyproject: Path) -> dict[str, str]:
    with pyproject.open("rb") as stream:
        data = tomllib.load(stream)
    scripts = data["project"].get("scripts", {})
    assert isinstance(scripts, dict)
    return scripts


def check_run_by_lane_scripts_kept(pyproject: Path, expected: dict[str, str]) -> None:
    """Raise AssertionError unless every expected script is declared as is."""
    assert expected, "no run_by_lane scripts to check; the source is empty"
    scripts = _scripts(pyproject)
    for name, target in expected.items():
        assert name in scripts, f"run_by_lane script {name} is gone from {pyproject}"
        assert scripts[name] == target, (
            f"run_by_lane script {name} targets {scripts[name]!r}, "
            f"the infra stack runs {target!r}"
        )


def check_customer_package_scripts(pyproject: Path, expected: dict[str, str]) -> None:
    """Raise AssertionError unless the customer package declares only its two."""
    scripts = _scripts(pyproject)
    assert set(scripts) == CUSTOMER_LAUNCHERS, sorted(scripts)
    assert not set(scripts) & set(expected), sorted(scripts)


@pytest.mark.unit
def test_run_by_lane_scripts_kept() -> None:
    check_run_by_lane_scripts_kept(MARKET_PYPROJECT, _expected_run_by_lane())


@pytest.mark.unit
def test_run_by_lane_literal_matches_infra_stack_shape() -> None:
    stack_shape = _find_infra_stack_shape()
    if stack_shape is None:
        pytest.skip("no sibling omnibase_infra checkout; the literal list stands alone")
    assert _derive_run_by_lane(stack_shape) == RUN_BY_LANE_LITERAL, (
        f"update RUN_BY_LANE_LITERAL to mirror {stack_shape}"
    )


@pytest.mark.unit
def test_customer_package_does_not_declare_run_by_lane_scripts() -> None:
    check_customer_package_scripts(PYPROJECT, _expected_run_by_lane())


def _scratch_pyproject(tmp_path: Path, source: Path, old: str, new: str) -> Path:
    text = source.read_text(encoding="utf-8")
    assert old in text, f"scratch edit target {old!r} not found"
    scratch = tmp_path / "pyproject.toml"
    scratch.write_text(text.replace(old, new, 1), encoding="utf-8")
    return scratch


@pytest.mark.unit
def test_removed_run_by_lane_script_is_caught(tmp_path: Path) -> None:
    name, target = next(iter(RUN_BY_LANE_LITERAL.items()))
    scratch = _scratch_pyproject(
        tmp_path, MARKET_PYPROJECT, f'{name} = "{target}"\n', ""
    )
    with pytest.raises(AssertionError, match="is gone"):
        check_run_by_lane_scripts_kept(scratch, RUN_BY_LANE_LITERAL)


@pytest.mark.unit
def test_retargeted_run_by_lane_script_is_caught(tmp_path: Path) -> None:
    name, target = next(iter(RUN_BY_LANE_LITERAL.items()))
    scratch = _scratch_pyproject(
        tmp_path, MARKET_PYPROJECT, f'{name} = "{target}"', f'{name} = "{target}_moved"'
    )
    with pytest.raises(AssertionError, match="targets"):
        check_run_by_lane_scripts_kept(scratch, RUN_BY_LANE_LITERAL)


@pytest.mark.unit
def test_run_by_lane_script_in_customer_package_is_caught(tmp_path: Path) -> None:
    name, target = next(iter(RUN_BY_LANE_LITERAL.items()))
    old = 'omni-agent = "omnibase_core.cli.cli_commands:cli"\n'
    scratch = _scratch_pyproject(
        tmp_path, PYPROJECT, old, old + f'{name} = "{target}"\n'
    )
    with pytest.raises(AssertionError):
        check_customer_package_scripts(scratch, RUN_BY_LANE_LITERAL)
