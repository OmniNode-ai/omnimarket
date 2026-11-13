# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""node_host_installer_registry_units: the command line is the installer's one entry point (OMN-24108).

``onex-host-install-units`` builds its request from the command line, the environment and the contract's
``command_line`` block, and runs it through the node. The seam tests tie that block to the request model and to the
console script declared in pyproject.toml, so either side changing shape fails here.
"""

from __future__ import annotations

import importlib
import json
import os
import subprocess
import tomllib
import typing
from pathlib import Path
from typing import Any

import pytest
import yaml
from omnibase_core.validators.no_unguarded_git_subprocess import scrub_git_location_env

from omnimarket.nodes.node_host_installer_registry_units import (
    runtime_registry_units_cli as cli,
)
from omnimarket.nodes.node_host_installer_registry_units.models.model_registry_units import (
    ModelRegistryUnitsRequest,
    ModelRegistryUnitsResult,
)

ROOT = Path(__file__).resolve().parents[3]
NODE_DIR = ROOT / "src" / "omnimarket" / "nodes" / "node_host_installer_registry_units"
SCRIPT = "onex-host-install-units"
ENTRY = "src/omnimarket/nodes/node_factory_floor_publish_effect/__main__.py"
HANDLER = "omnimarket.nodes.node_host_installer_registry_units.handlers.handler_registry_units"


def contract() -> dict[str, Any]:
    document: dict[str, Any] = yaml.safe_load(
        (NODE_DIR / "contract.yaml").read_text(encoding="utf-8")
    )
    return document


def _git(path: Path, *args: str) -> str:
    done = subprocess.run(
        [
            "git",
            "-C",
            str(path),
            "-c",
            "user.name=t",
            "-c",
            "user.email=t@example.com",
            *args,
        ],
        env=scrub_git_location_env(os.environ),
        capture_output=True,
        text=True,
        check=True,
    )
    return done.stdout.strip()


@pytest.fixture
def checkout(tmp_path: Path) -> tuple[Path, str]:
    path = tmp_path / "branch"
    (path / ENTRY).parent.mkdir(parents=True)
    (path / ENTRY).write_text("print('tick')\n", encoding="utf-8")
    _git(path, "init", "-q", "-b", "fix")
    _git(path, "add", "-A")
    _git(path, "commit", "-q", "-m", "tick")
    return path, _git(path, "rev-parse", "HEAD")


@pytest.fixture(autouse=True)
def brew_python_present(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        f"{HANDLER}.interpreter_present",
        lambda path: path == "/opt/homebrew/bin/python3.13",
    )


def test_the_request_takes_the_registry_root_and_inputs_from_the_environment_and_no_default_names_a_private_path() -> (
    None
):
    request = cli.build_request(
        ["factory-floor-publish", "--dry-run"],
        {"OMNI_HOME": "/reg", "ONEX_WORKSPACE_CONFIG_ROOT": "/ws"},
        "/h",
    )
    assert request.registry_root == "/reg"
    assert request.workspace_config_root == "/ws"
    assert request.plans_path == ""
    assert request.marketplace_root == ""
    assert request.mode == "dry-run"
    assert request.home == "/h"


def test_a_flag_beats_the_environment_and_the_environment_beats_the_default() -> None:
    request = cli.build_request(
        ["u", "--registry-root", "/flag", "--plans-path", "/plans"],
        {
            "OMNI_HOME": "/env",
            "ONEX_MARKETPLACE_ROOT": "/market",
            "ONEX_PLANS_KB_PATH": "/env-plans",
        },
        "/h",
    )
    assert request.registry_root == "/flag"
    assert request.marketplace_root == "/market"
    assert request.plans_path == "/plans"
    env_only = cli.build_request(
        ["u"], {"OMNI_HOME": "/env", "ONEX_PLANS_KB_PATH": "/env-plans"}, "/h"
    )
    assert env_only.plans_path == "/env-plans"


def test_every_mode_flag_selects_its_mode_and_the_last_flag_wins() -> None:
    modes = contract()["command_line"]["modes"]
    for mode in modes:
        request = cli.build_request(["u", f"--{mode}"], {"OMNI_HOME": "/r"}, "/h")
        assert request.mode == mode
    request = cli.build_request(
        ["u", "--install", "--rollback"], {"OMNI_HOME": "/r"}, "/h"
    )
    assert request.mode == "rollback"
    bare = cli.build_request(["u"], {"OMNI_HOME": "/r"}, "/h")
    assert bare.mode == ModelRegistryUnitsRequest.model_fields["mode"].default


def test_the_contract_command_line_and_the_request_model_and_the_console_script_agree() -> (
    None
):
    command_line = contract()["command_line"]
    mode_field = ModelRegistryUnitsRequest.model_fields["mode"].annotation
    assert set(command_line["modes"]) == set(typing.get_args(mode_field))
    fields = set(ModelRegistryUnitsRequest.model_fields)
    assert set(command_line["env_inputs"]) <= fields
    assert set(command_line["defaults"]) <= fields
    assert set(command_line["required_inputs"]) <= fields
    assert command_line["program"] == SCRIPT
    scripts = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))[
        "project"
    ]["scripts"]
    module, _, attribute = scripts[SCRIPT].partition(":")
    assert (
        module
        == "omnimarket.nodes.node_host_installer_registry_units.runtime_registry_units_cli"
    )
    assert callable(getattr(importlib.import_module(module), attribute))


def test_set_options_become_path_overrides_and_one_without_an_equals_is_refused() -> (
    None
):
    request = cli.build_request(
        ["u", "--set", "A=/a", "--set", "B=/b"], {"OMNI_HOME": "/r"}, "/h"
    )
    assert request.path_overrides == {"A": "/a", "B": "/b"}
    with pytest.raises(SystemExit) as refused:
        cli.build_request(["u", "--set", "NOEQUALS"], {"OMNI_HOME": "/r"}, "/h")
    assert refused.value.code == 2


def test_a_missing_registry_root_is_refused_naming_the_variable_and_the_option(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(ValueError, match=r"OMNI_HOME.*--registry-root"):
        cli.build_request(["u"], {}, "/h")
    monkeypatch.delenv("OMNI_HOME", raising=False)
    assert cli.main(["factory-floor-publish", "--dry-run"]) == 1
    assert capsys.readouterr().err.startswith(f"{SCRIPT}: set OMNI_HOME")


def test_list_units_prints_every_unit_of_the_contract(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert cli.main(["--list-units"]) == 0
    printed = {line.split()[0] for line in capsys.readouterr().out.splitlines()}
    assert printed == set(contract()["units"])


def golden_arguments(
    tmp_path: Path, branch: Path, revision: str, *extra: str
) -> list[str]:
    return [
        "factory-floor-publish",
        *extra,
        "--registry-root",
        str(tmp_path / "root"),
        "--workspace-config-root",
        str(tmp_path / "ws"),
        "--plans-path",
        str(tmp_path / "plans"),
        "--marketplace-root",
        str(tmp_path / "market"),
        "--project-path",
        str(branch),
        "--expected-revision",
        revision,
        "--host-name",
        "lab-host",
    ]


def test_golden_chain_the_command_renders_the_unit_for_the_branch_checkout_through_the_node(
    tmp_path: Path,
    checkout: tuple[Path, str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    branch, revision = checkout
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    code = cli.main(golden_arguments(tmp_path, branch, revision, "--dry-run"))
    out = capsys.readouterr()
    assert code == 0, out.err
    assert f"<string>{branch}</string>" in out.out
    assert "ONEX_LAB_RUN_HOSTS" in out.out
    assert not (tmp_path / "home" / "Library").exists()


def test_golden_chain_json_prints_the_typed_result(
    tmp_path: Path,
    checkout: tuple[Path, str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    branch, revision = checkout
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    assert (
        cli.main([*golden_arguments(tmp_path, branch, revision, "--dry-run"), "--json"])
        == 0
    )
    result = ModelRegistryUnitsResult.model_validate_json(capsys.readouterr().out)
    assert result.status == "success"
    assert result.revision == revision
    assert json.loads(result.model_dump_json())["mode"] == "dry-run"


def test_error_chain_an_unknown_unit_exits_two_and_says_it_is_not_a_unit_of_the_node(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    code = cli.main(["no-such-unit", "--dry-run", "--registry-root", str(tmp_path)])
    assert code == 2
    assert "is not a unit of this node" in capsys.readouterr().err


def test_error_chain_a_rollback_with_nothing_kept_exits_one_through_the_node(
    tmp_path: Path,
    checkout: tuple[Path, str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    branch, revision = checkout
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    code = cli.main(golden_arguments(tmp_path, branch, revision, "--rollback"))
    assert code == 1
    assert "nothing to roll back" in capsys.readouterr().err
