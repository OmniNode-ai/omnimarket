# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Scratch-home shell safety: no user profile is sourced or edited by this suite.

Failures: conflict missed, default mutation, executed profile, secret disclosure,
unsafe multiline/symlink edit, lost concurrent user change, repeated backup, and
persisting one invocation's recovery consent into later setup runs.
"""

from __future__ import annotations

import importlib
import json
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from omnimarket.nodes.node_onex_setup_effect.handlers.handler_setup_environment import (
    HandlerSetupEnvironment,
)
from omnimarket.nodes.node_onex_setup_effect.handlers.handler_setup_workflow import (
    HandlerSetupWorkflow,
)
from omnimarket.nodes.node_onex_setup_effect.models.model_setup_plan import (
    ModelSetupPlan,
)

pytestmark = pytest.mark.e2e


def _owner(home: Path, **values: object) -> object:
    module = importlib.import_module(
        "omnimarket.nodes.node_onex_setup_effect.handlers.handler_setup_shell"
    )
    return module.HandlerSetupShell(
        onex_home=home / ".onex",
        home=home,
        environment={"PATH": str(Path(sys.executable).parent)},
        **values,
    )


def _plan(home: Path, **values: object) -> ModelSetupPlan:
    return ModelSetupPlan.model_validate(
        {"workspace": home / "workspace", "components": ["delegation"], **values}
    )


def test_default_conflicts_stop_before_workspace_and_never_execute_profiles(
    tmp_path: Path,
) -> None:
    profile = tmp_path / ".bashrc"
    profile.write_text(
        "alias onex='planted-secret-value'\nprintf executed > '"
        + str(tmp_path / "executed")
        + "'\n"
    )
    before = profile.read_bytes(), profile.stat().st_mtime_ns
    journal = HandlerSetupWorkflow(
        onex_home=tmp_path / ".onex",
        steps=(
            _owner(tmp_path),
            HandlerSetupEnvironment(name="workspace", onex_home=tmp_path / ".onex"),
        ),
    ).handle(_plan(tmp_path))
    result = journal.observations[0]
    assert result.step == "shell-safety"
    assert result.status == "action_required"
    assert ".bashrc" in result.model_dump_json()
    assert "planted-secret-value" not in result.model_dump_json()
    assert not (tmp_path / "workspace").exists()
    assert not (tmp_path / "executed").exists()
    assert (profile.read_bytes(), profile.stat().st_mtime_ns) == before
    assert not list(tmp_path.rglob("*.backup"))


def test_explicit_recovery_is_backed_up_idempotent_and_does_not_persist_consent(
    tmp_path: Path,
) -> None:
    profile = tmp_path / ".bashrc"
    original = (
        "# keep\nexport KEEP=untouched\nalias onex='old-onex'\nexport OMNIBASE_PATH='/old-secret-workspace'\nprintf executed > '"
        + str(tmp_path / "executed")
        + "'\n"
    )
    profile.write_text(original)
    plan = _plan(tmp_path, set_aside=True)

    def workflow() -> HandlerSetupWorkflow:
        return HandlerSetupWorkflow(
            onex_home=tmp_path / ".onex", steps=(_owner(tmp_path),)
        )

    first = workflow().handle(plan)
    assert first.observations[0].status == "success", first.model_dump_json()
    assert first.observations[0].changed
    backups = list((tmp_path / ".onex").rglob("*.backup"))
    assert len(backups) == 1
    assert backups[0].read_text() == original
    assert stat.S_IMODE(backups[0].stat().st_mode) == 0o600
    assert "export KEEP=untouched" in profile.read_text()
    assert "old-secret-workspace" not in profile.read_text()
    assert not (tmp_path / "executed").exists()
    assert "old-secret-workspace" not in first.model_dump_json()
    before = profile.read_bytes(), profile.stat().st_mtime_ns
    again = workflow().handle(plan)
    assert again.observations[0].status == "success"
    assert not again.observations[0].changed
    assert (profile.read_bytes(), profile.stat().st_mtime_ns) == before
    assert list((tmp_path / ".onex").rglob("*.backup")) == backups
    saved = HandlerSetupWorkflow.saved_plan(tmp_path / ".onex")
    assert saved is not None
    assert not saved.set_aside
    profile.write_text(profile.read_text() + "alias onex='later-user-choice'\n")
    changed = profile.read_bytes()
    assert workflow().handle(saved).observations[0].status == "action_required"
    assert profile.read_bytes() == changed
    (tmp_path / "shell-recovery-proof.json").write_text(
        json.dumps(
            {
                "first": first.model_dump(mode="json"),
                "repeat": again.model_dump(mode="json"),
                "backups": [str(path) for path in backups],
                "profiles_executed": False,
            },
            indent=2,
        )
    )


@pytest.mark.parametrize(
    "content",
    [
        "onex() {\n echo secret\n}\n",
        "export OMNIBASE_PATH=old; echo unrelated\n",
        "alias onex='unterminated\n",
        "export OMNIBASE_PATH=old\\\nmore\n",
    ],
)
def test_ambiguous_or_invalid_shell_is_preserved(tmp_path: Path, content: str) -> None:
    profile = tmp_path / ".bashrc"
    profile.write_text(content)
    safe = tmp_path / ".profile"
    safe.write_text("alias onex='old'\n")
    result = _owner(tmp_path).apply(_plan(tmp_path, set_aside=True))
    assert result.status == "action_required"
    assert profile.read_text() == content
    assert safe.read_text() == "alias onex='old'\n"
    assert not list(tmp_path.rglob("*.backup"))


def test_symlink_and_shadowed_executable_are_named_but_untouched(
    tmp_path: Path,
) -> None:
    other = tmp_path / "unrelated"
    other.write_text("alias onex='old'\n")
    (tmp_path / ".bashrc").symlink_to(other)
    result = _owner(tmp_path).observe(_plan(tmp_path, set_aside=True))
    assert result.status == "action_required"
    assert other.read_text() == "alias onex='old'\n"
    shadow = tmp_path / "bin"
    shadow.mkdir()
    executable = shadow / "onex"
    executable.write_text("#!/bin/sh\ntouch '" + str(tmp_path / "executed") + "'\n")
    executable.chmod(0o700)
    module = importlib.import_module(
        "omnimarket.nodes.node_onex_setup_effect.handlers.handler_setup_shell"
    )
    owner = module.HandlerSetupShell(
        home=tmp_path, onex_home=tmp_path / ".onex", environment={"PATH": str(shadow)}
    )
    result = owner.apply(_plan(tmp_path, set_aside=True))
    assert result.status == "action_required"
    assert "executable" in result.model_dump_json()
    assert executable.exists()
    assert not (tmp_path / "executed").exists()


def test_concurrent_profile_change_is_not_overwritten(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = importlib.import_module(
        "omnimarket.nodes.node_onex_setup_effect.setup_shell_profiles"
    )
    profile = tmp_path / ".bashrc"
    profile.write_text("alias onex='old'\n")
    validate = module.validate_syntax

    def race(content: bytes, shell: str) -> None:
        validate(content, shell)
        profile.write_text("# later user content\n")

    monkeypatch.setattr(module, "validate_syntax", race)
    result = _owner(tmp_path).apply(_plan(tmp_path, set_aside=True))
    assert result.status == "action_required"
    assert profile.read_text() == "# later user content\n"


def test_setup_cli_set_aside_is_explicit_and_verify_never_edits(tmp_path: Path) -> None:
    profile = tmp_path / ".bashrc"
    profile.write_text("alias onex='old-shell-command'\n")
    cli = str(Path(sys.executable).parent / "onex")
    environment = {
        "HOME": str(tmp_path),
        "PATH": str(Path(cli).parent) + ":/usr/bin:/bin",
        "ONEX_STATE_DIR": str(tmp_path / "state"),
    }
    arguments = [
        cli,
        "setup",
        "--component",
        "local-stack",
        "--workspace",
        str(tmp_path / "workspace"),
        "--no-interactive",
        "--json",
        "--yes",
    ]
    before = profile.read_bytes()
    default = subprocess.run(
        arguments,
        env=environment,
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=45,
        check=False,
    )
    journal = json.loads(default.stdout)
    assert journal["observations"][-1]["step"] == "shell-safety"
    assert journal["observations"][-1]["status"] == "action_required"
    assert profile.read_bytes() == before
    assert not (tmp_path / "workspace").exists()
    verified = subprocess.run(
        [*arguments, "--set-aside", "--action", "verify"],
        env=environment,
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=45,
        check=False,
    )
    assert verified.stdout.startswith("{"), verified.stdout + verified.stderr
    assert profile.read_bytes() == before
    recovered = subprocess.run(
        [*arguments, "--set-aside"],
        env=environment,
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=45,
        check=False,
    )
    result = json.loads(recovered.stdout)
    shell = next(
        item for item in result["observations"] if item["step"] == "shell-safety"
    )
    assert shell["status"] == "success", result
    assert list((tmp_path / ".onex").rglob("*.backup"))
    assert "set_aside" not in result["plan"]
