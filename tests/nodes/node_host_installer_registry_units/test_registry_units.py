# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""node_host_installer_registry_units: a registry_module_tick unit runs a branch checkout when its contract declares the override."""

from __future__ import annotations

import importlib
import json
import os
import plistlib
import subprocess
import tomllib
from pathlib import Path
from typing import Any

import pytest
import yaml
from omnibase_core.validators.no_unguarded_git_subprocess import scrub_git_location_env

from omnimarket.nodes.node_host_installer_registry_units.handlers.handler_registry_units import (
    HandlerRegistryUnits,
    load_units,
)
from omnimarket.nodes.node_host_installer_registry_units.models.model_registry_units import (
    ModelRegistryUnitsRequest,
)

ROOT = Path(__file__).resolve().parents[3]
NODE_DIR = ROOT / "src" / "omnimarket" / "nodes" / "node_host_installer_registry_units"
UNIT = "factory-floor-publish"


def _run(argv: list[str], **kw: Any) -> subprocess.CompletedProcess[str]:
    return subprocess.run(argv, env=scrub_git_location_env(os.environ), **kw)


def real_run(argv: list[str]) -> subprocess.CompletedProcess[str]:
    """Real git and id; every launchctl and uv proof is recorded and answered with success."""
    if argv[0] in ("git", "id"):
        return _run(argv, capture_output=True, text=True, check=False)
    return subprocess.CompletedProcess(argv, 0, "", "")


class Recorder:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def __call__(self, argv: list[str]) -> subprocess.CompletedProcess[str]:
        self.calls.append(argv)
        return real_run(argv)


def make_checkout(
    path: Path,
    entry: str = "src/omnimarket/nodes/node_factory_floor_publish_effect/__main__.py",
) -> str:
    path.mkdir(parents=True)
    _run(["git", "-C", str(path), "init", "-q", "-b", "branch"], check=True)
    (path / entry).parent.mkdir(parents=True)
    (path / entry).write_text("print('tick')\n", encoding="utf-8")
    _run(["git", "-C", str(path), "add", "-A"], check=True)
    _run(
        [
            "git",
            "-C",
            str(path),
            "-c",
            "user.name=t",
            "-c",
            "user.email=t@example.com",
            "commit",
            "-q",
            "-m",
            "x",
        ],
        check=True,
    )
    return _run(
        ["git", "-C", str(path), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def request(tmp_path: Path, **kw: Any) -> ModelRegistryUnitsRequest:
    fields: dict[str, Any] = {
        "unit": UNIT,
        "home": str(tmp_path / "home"),
        "registry_root": str(tmp_path / "omni"),
        "workspace_config_root": str(tmp_path / "ws"),
        "plans_path": str(tmp_path / "plans"),
        "marketplace_root": str(tmp_path / "market"),
    }
    fields.update(kw)
    return ModelRegistryUnitsRequest(**fields)


@pytest.fixture
def canonical(tmp_path: Path) -> Path:
    path = tmp_path / "omni" / "omnimarket"
    make_checkout(path)
    return path


def plist_of(text: str) -> dict[str, Any]:
    loaded: dict[str, Any] = plistlib.loads(text.encode("utf-8"))
    return loaded


def test_without_an_override_the_unit_runs_the_canonical_clone(
    tmp_path: Path, canonical: Path
) -> None:
    result = HandlerRegistryUnits(real_run).handle(request(tmp_path))
    assert result.status == "success", result.stderr
    assert result.project_is_canonical
    args = plist_of(result.rendered)["ProgramArguments"]
    assert args[args.index("--project") + 1] == str(canonical)


def test_with_the_override_the_rendered_unit_runs_the_branch_checkout_and_not_the_canonical_clone(
    tmp_path: Path, canonical: Path
) -> None:
    branch = tmp_path / "branch"
    revision = make_checkout(branch)
    result = HandlerRegistryUnits(real_run).handle(
        request(tmp_path, project_path=str(branch), expected_revision=revision)
    )
    assert result.status == "success", result.stderr
    assert not result.project_is_canonical
    assert result.revision == revision
    args = plist_of(result.rendered)["ProgramArguments"]
    assert args[args.index("--project") + 1] == str(branch)
    # the falsifier: no argument of the rendered unit may still name the canonical clone path
    assert all(str(canonical) not in arg for arg in args)


def test_an_override_that_is_dirty_at_the_wrong_revision_or_without_the_entry_file_is_refused(
    tmp_path: Path, canonical: Path
) -> None:
    branch = tmp_path / "branch"
    revision = make_checkout(branch)
    handler = HandlerRegistryUnits(real_run)
    wrong = handler.handle(
        request(tmp_path, project_path=str(branch), expected_revision="0" * 12)
    )
    assert wrong.status == "failure"
    assert "not the expected revision" in wrong.stderr
    (branch / "stray.txt").write_text("x", encoding="utf-8")
    dirty = handler.handle(
        request(tmp_path, project_path=str(branch), expected_revision=revision)
    )
    assert dirty.status == "failure"
    assert "not clean" in dirty.stderr
    bare = tmp_path / "bare"
    make_checkout(bare, entry="other/file.py")
    absent = handler.handle(request(tmp_path, project_path=str(bare)))
    assert absent.status == "failure"
    assert "does not carry" in absent.stderr
    relative = handler.handle(request(tmp_path, project_path="branch"))
    assert relative.status == "failure"
    assert "not an absolute path" in relative.stderr


def test_a_unit_that_declares_no_override_refuses_one(
    tmp_path: Path, canonical: Path
) -> None:
    units = load_units()
    units[UNIT].pop("project_path_override")
    result = HandlerRegistryUnits(real_run, units).handle(
        request(tmp_path, project_path=str(tmp_path / "branch"))
    )
    assert result.status == "failure"
    assert "declares no project path override" in result.stderr


def test_only_a_declared_environment_path_may_be_overridden(
    tmp_path: Path, canonical: Path
) -> None:
    handler = HandlerRegistryUnits(real_run)
    ok = handler.handle(
        request(
            tmp_path, path_overrides={"ONEX_SKILL_OVERLAY_ROOTS": "/installed/overlays"}
        )
    )
    assert (
        plist_of(ok.rendered)["EnvironmentVariables"]["ONEX_SKILL_OVERLAY_ROOTS"]
        == "/installed/overlays"
    )
    refused = handler.handle(
        request(tmp_path, path_overrides={"ONEX_LEDGER_PATH": "/elsewhere"})
    )
    assert refused.status == "failure"
    assert "not a path override" in refused.stderr


def test_a_placeholder_the_request_does_not_set_is_refused(
    tmp_path: Path, canonical: Path
) -> None:
    result = HandlerRegistryUnits(real_run).handle(
        request(tmp_path, workspace_config_root="")
    )
    assert result.status == "failure"
    assert "workspace_config_root" in result.stderr


def test_install_writes_the_plist_loads_it_and_records_the_revision_it_runs(
    tmp_path: Path, canonical: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    branch = tmp_path / "branch"
    revision = make_checkout(branch)
    monkeypatch.setattr(
        "shutil.which", lambda name: "/usr/bin/uv" if name == "uv" else None
    )
    run = Recorder()
    result = HandlerRegistryUnits(run).handle(
        request(
            tmp_path,
            mode="install",
            project_path=str(branch),
            expected_revision=revision,
        )
    )
    assert result.status == "success", result.stderr
    plist = (
        tmp_path
        / "home"
        / "Library/LaunchAgents"
        / "ai.omninode.factory-floor-publish.plist"
    )
    args = plist_of(plist.read_text(encoding="utf-8"))["ProgramArguments"]
    assert args[args.index("--project") + 1] == str(branch)
    record = json.loads(Path(result.record).read_text(encoding="utf-8"))
    assert record["revision"] == revision
    assert record["project"] == str(branch)
    assert not record["project_is_canonical"]
    verbs = [c[1] for c in run.calls if c[0] == "launchctl"]
    assert verbs == ["bootout", "bootstrap"]
    proofs = [c for c in run.calls if c[0] == "/usr/bin/uv"]
    assert proofs
    assert str(branch) in proofs[0]


def test_an_unknown_unit_is_refused(tmp_path: Path) -> None:
    result = HandlerRegistryUnits(real_run).handle(
        request(tmp_path, unit="no-such-unit")
    )
    assert result.status == "failure"
    assert result.exit_code == 2


def test_the_contract_declares_the_override_and_the_node_is_registered() -> None:
    contract = yaml.safe_load((NODE_DIR / "contract.yaml").read_text(encoding="utf-8"))
    assert contract["name"] == "node_host_installer_registry_units"
    unit = contract["units"][UNIT]
    assert unit["kind"] == "registry_module_tick"
    assert unit["project_path_override"]["request_field"] == "project_path"
    assert hasattr(
        importlib.import_module(contract["handler"]["module"]),
        contract["handler"]["name"],
    )
    assert hasattr(
        importlib.import_module(contract["entrypoint"]["module"]),
        contract["entrypoint"]["name"],
    )
    assert (NODE_DIR / unit["template"]).is_file()
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert (
        pyproject["project"]["entry-points"]["onex.nodes"][
            "node_host_installer_registry_units"
        ]
        == "omnimarket.nodes.node_host_installer_registry_units"
    )
    assert (
        yaml.safe_load((NODE_DIR / "metadata.yaml").read_text(encoding="utf-8"))["name"]
        == contract["name"]
    )
