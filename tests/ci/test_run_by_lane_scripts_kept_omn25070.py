# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The console scripts the infra stack runs stay declared (OMN-25070, AC5).

AC5 (amended 2026-11-18): T1 removes no console script marked ``run_by_lane``.
omnibase_infra's ``config/validation/stack_shape.yaml`` lists every omnimarket
console script with a status; a ``run_by_lane`` one is commanded by a compose
carrier, so dropping it from ``[project.scripts]`` breaks a running stack. The
customer package ``omninode-onex`` must not take them over either.

Failure modes each test catches: a run_by_lane script deleted from
pyproject.toml; one whose target was changed; one added to the customer
package; the literal fallback list drifting from the infra stack shape (the
fallback is what hosted CI checks, where no omnibase_infra checkout exists).
The negative tests run the same checker against a scratch copy of
pyproject.toml, so each is shown to fail without touching the real file.
"""

from __future__ import annotations

import os
import tomllib
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
PYPROJECT = REPO_ROOT / "pyproject.toml"
CUSTOMER_PYPROJECT = REPO_ROOT / "packages" / "omninode-onex" / "pyproject.toml"
STACK_SHAPE_RELATIVE = Path("config") / "validation" / "stack_shape.yaml"
RUN_BY_LANE = "run_by_lane"

# Mirrors omnibase_infra dev config/validation/stack_shape.yaml, console_scripts
# -> omnimarket -> omnimarket-projection-api (status: run_by_lane), read
# 2026-11-20 around lines 684-688. When a sibling checkout exists,
# test_literal_list_matches_infra_stack_shape fails if this drifts.
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
    assert set(scripts) == {"onex", "omni-agent"}, sorted(scripts)
    assert not set(scripts) & set(expected), sorted(scripts)


@pytest.mark.unit
def test_run_by_lane_scripts_kept() -> None:
    check_run_by_lane_scripts_kept(PYPROJECT, _expected_run_by_lane())


@pytest.mark.unit
def test_literal_list_matches_infra_stack_shape() -> None:
    stack_shape = _find_infra_stack_shape()
    if stack_shape is None:
        pytest.skip("no sibling omnibase_infra checkout; the literal list stands alone")
    assert _derive_run_by_lane(stack_shape) == RUN_BY_LANE_LITERAL, (
        f"update RUN_BY_LANE_LITERAL to mirror {stack_shape}"
    )


@pytest.mark.unit
def test_customer_package_does_not_declare_run_by_lane_scripts() -> None:
    check_customer_package_scripts(CUSTOMER_PYPROJECT, _expected_run_by_lane())


def _scratch_pyproject(tmp_path: Path, source: Path, old: str, new: str) -> Path:
    text = source.read_text(encoding="utf-8")
    assert old in text, f"scratch edit target {old!r} not found"
    scratch = tmp_path / "pyproject.toml"
    scratch.write_text(text.replace(old, new, 1), encoding="utf-8")
    return scratch


@pytest.mark.unit
def test_removed_run_by_lane_script_is_caught(tmp_path: Path) -> None:
    name, target = next(iter(RUN_BY_LANE_LITERAL.items()))
    scratch = _scratch_pyproject(tmp_path, PYPROJECT, f'{name} = "{target}"\n', "")
    with pytest.raises(AssertionError, match="is gone"):
        check_run_by_lane_scripts_kept(scratch, RUN_BY_LANE_LITERAL)


@pytest.mark.unit
def test_retargeted_run_by_lane_script_is_caught(tmp_path: Path) -> None:
    name, target = next(iter(RUN_BY_LANE_LITERAL.items()))
    scratch = _scratch_pyproject(
        tmp_path, PYPROJECT, f'{name} = "{target}"', f'{name} = "{target}_moved"'
    )
    with pytest.raises(AssertionError, match="targets"):
        check_run_by_lane_scripts_kept(scratch, RUN_BY_LANE_LITERAL)


@pytest.mark.unit
def test_run_by_lane_script_in_customer_package_is_caught(tmp_path: Path) -> None:
    name, target = next(iter(RUN_BY_LANE_LITERAL.items()))
    old = 'omni-agent = "omnibase_core.cli.cli_commands:cli"\n'
    scratch = _scratch_pyproject(
        tmp_path, CUSTOMER_PYPROJECT, old, old + f'{name} = "{target}"\n'
    )
    with pytest.raises(AssertionError):
        check_customer_package_scripts(scratch, RUN_BY_LANE_LITERAL)
