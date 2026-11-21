# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A stale or incomplete CLI installation cannot prove the current hook revision."""

from __future__ import annotations

import os
import shutil
import subprocess
import venv
from pathlib import Path

import pytest
from omnibase_core.validators.no_unguarded_git_subprocess import (
    scrub_git_location_env,
)

from omnimarket.nodes.node_hook_delegate_effect.tests.test_hook_live import (
    installed_provenance,
)


@pytest.fixture
def candidate(tmp_path: Path) -> tuple[Path, str, Path, Path]:
    repository = tmp_path / "source"
    package = repository / "src" / "omnimarket"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text('"""Fixture installed package."""\n')
    (package / "behavior.py").write_text('ANSWER = "current"\n')
    subprocess.run(
        ["git", "init", "-q", str(repository)],
        check=True,
        env=scrub_git_location_env(os.environ),
    )
    subprocess.run(
        ["git", "-C", str(repository), "add", "src"],
        check=True,
        env=scrub_git_location_env(os.environ),
    )
    revision = subprocess.check_output(
        ["git", "-C", str(repository), "write-tree"],
        text=True,
        env=scrub_git_location_env(os.environ),
    ).strip()
    environment = tmp_path / "installed"
    venv.EnvBuilder(symlinks=True).create(environment)
    python = environment / "bin" / "python"
    site = Path(
        subprocess.check_output(
            [
                str(python),
                "-I",
                "-c",
                "import sysconfig; print(sysconfig.get_path('purelib'))",
            ],
            text=True,
        ).strip()
    )
    installed = site / "omnimarket"
    shutil.copytree(package, installed)
    launcher = environment / "bin" / "onex"
    launcher.write_text(f"#!{python}\n# Fixture launcher; never performs inference.\n")
    launcher.chmod(0o700)
    return repository, revision, launcher, installed


def test_current_installed_source_is_bound_to_the_revision(
    candidate: tuple[Path, str, Path, Path],
) -> None:
    repository, revision, launcher, installed = candidate
    proof = installed_provenance(launcher, revision, repository)
    assert Path(proof["package_root"]) == installed
    assert Path(proof["launcher"]) == launcher
    assert proof["revision"] == revision
    assert proof["files_checked"] == 2
    assert len(proof["source_manifest_sha256"]) == 64


@pytest.mark.parametrize("fault", ["stale", "missing"])
def test_stale_or_incomplete_installation_is_refused_before_inference(
    candidate: tuple[Path, str, Path, Path],
    fault: str,
) -> None:
    repository, revision, launcher, installed = candidate
    behavior = installed / "behavior.py"
    if fault == "stale":
        behavior.write_text('ANSWER = "previous"\n')
    else:
        behavior.unlink()
    with pytest.raises(AssertionError, match="installed package differs from revision"):
        installed_provenance(launcher, revision, repository)


def test_extra_package_cannot_shadow_committed_module(
    candidate: tuple[Path, str, Path, Path],
) -> None:
    repository, revision, launcher, installed = candidate
    python = launcher.read_text().splitlines()[0][2:]
    command = [
        python,
        "-I",
        "-c",
        "from omnimarket.behavior import ANSWER; print(ANSWER)",
    ]
    before = subprocess.check_output(command, text=True).strip()
    assert before == "current"
    shadow = installed / "behavior"
    shadow.mkdir()
    (shadow / "__init__.py").write_text('ANSWER = "stale-extra-package"\n')
    after = subprocess.check_output(command, text=True).strip()
    assert after == "stale-extra-package"
    with pytest.raises(AssertionError, match="installed package differs from revision"):
        installed_provenance(launcher, revision, repository)


def test_generated_bytecode_does_not_change_source_manifest(
    candidate: tuple[Path, str, Path, Path],
) -> None:
    repository, revision, launcher, installed = candidate
    before = installed_provenance(launcher, revision, repository)
    python = launcher.read_text().splitlines()[0][2:]
    subprocess.run([python, "-I", "-c", "import omnimarket.behavior"], check=True)
    assert list(installed.rglob("__pycache__/*.pyc"))
    after = installed_provenance(launcher, revision, repository)
    assert before["source_manifest_sha256"] == after["source_manifest_sha256"]


@pytest.mark.parametrize(
    "resource", ["config/ci_bus_lanes.yaml", "dashboard_bundle_pin.toml"]
)
def test_wheel_added_data_does_not_change_tracked_source_proof(
    candidate: tuple[Path, str, Path, Path],
    resource: str,
) -> None:
    repository, revision, launcher, installed = candidate
    before = installed_provenance(launcher, revision, repository)
    added = installed / resource
    added.parent.mkdir(parents=True, exist_ok=True)
    added.write_text("# Wheel force-included data, outside src/omnimarket\n")
    after = installed_provenance(launcher, revision, repository)
    assert after["source_manifest_sha256"] == before["source_manifest_sha256"]
    assert after["files_checked"] == before["files_checked"]
