# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Installed setup uses the typed engine and observes without writing.

Failure modes: flags accepted but ignored, verify provisioning, malformed plans,
secret values echoed by validation, stale legacy completion bypassing observation.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from omnimarket.nodes.node_onex_setup_effect.models.model_setup_journal import (
    ModelSetupJournal,
)

pytestmark = pytest.mark.e2e


def _snapshot(root: Path) -> dict[str, bytes]:
    return {
        str(p.relative_to(root)): p.read_bytes() if p.is_file() else b"DIR"
        for p in root.rglob("*")
    }


def test_local_model_verify_uses_the_same_engine_without_installing(
    tmp_path: Path,
) -> None:
    home, workspace = tmp_path / "home", tmp_path / "workspace"
    home.mkdir()
    workspace.mkdir()
    before = _snapshot(tmp_path)
    result = subprocess.run(
        (
            str(Path(sys.executable).parent / "onex"),
            "setup",
            "--action",
            "verify",
            "--json",
            "--no-interactive",
            "--workspace",
            str(workspace),
            "--component",
            "delegation",
            "--provider",
            "local",
            "--model",
            "user-managed-model:custom",
            "--url",
            "http://127.0.0.1:11434/v1",
        ),
        env={
            "HOME": str(home),
            "PATH": str(Path(sys.executable).parent)
            + os.pathsep
            + os.environ.get("PATH", "/usr/bin:/bin"),
            "ONEX_STATE_DIR": str(tmp_path / "state"),
        },
        cwd=workspace,
        capture_output=True,
        text=True,
        input="",
        timeout=45,
    )
    assert result.stdout.startswith("{"), result.stdout + result.stderr
    journal = ModelSetupJournal.model_validate_json(result.stdout)
    assert "models" in {item.step for item in journal.observations}
    assert all(not item.changed for item in journal.observations)
    assert _snapshot(tmp_path) == before


@pytest.mark.parametrize("plan_file", [False, True])
def test_installed_verify_observes_empty_install_without_writes(
    tmp_path: Path, plan_file: bool
) -> None:
    home, workspace = tmp_path / "home", tmp_path / "workspace with spaces"
    home.mkdir()
    workspace.mkdir()
    argv = [
        str(Path(sys.executable).parent / "onex"),
        "setup",
        "--json",
        "--no-interactive",
    ]
    if plan_file:
        path = tmp_path / "plan.json"
        path.write_text(
            json.dumps(
                {
                    "workspace": str(workspace),
                    "action": "verify",
                    "components": ["delegation"],
                }
            )
        )
        argv += ["--plan", str(path)]
    else:
        argv += [
            "--action",
            "verify",
            "--workspace",
            str(workspace),
            "--component",
            "delegation",
        ]
    before = _snapshot(tmp_path)
    result = subprocess.run(
        argv,
        env={
            "HOME": str(home),
            "PATH": str(Path(sys.executable).parent)
            + os.pathsep
            + os.environ.get("PATH", "/usr/bin:/bin"),
            "ONEX_STATE_DIR": str(tmp_path / "state"),
            **(
                {"OMNIBASE_PATH": str(tmp_path / "inherited-other-workspace")}
                if plan_file
                else {}
            ),
        },
        cwd=workspace,
        capture_output=True,
        text=True,
        input="",
        timeout=30,
    )
    assert result.returncode == 1, result.stdout + result.stderr
    journal = ModelSetupJournal.model_validate(json.loads(result.stdout))
    assert journal.schema_version == 2
    assert journal.workspace == workspace.resolve()
    observations = {item.step: item for item in journal.observations}
    if plan_file:
        # An inherited OMNIBASE_PATH naming another workspace would point every
        # other tool in that shell elsewhere, so verify names it and stops there.
        safety = observations["shell-safety"]
        assert safety.status == "action_required"
        assert "OMNIBASE_PATH" in safety.model_dump_json()
        assert not any(item.changed for item in journal.observations)
        assert _snapshot(tmp_path) == before
        return
    assert {
        "preflight",
        "shell-safety",
        "workspace",
        "user_config",
        "runtime_config",
        "identity",
        "models",
    } <= observations.keys()
    assert observations["workspace"].status == "unavailable"
    assert observations["identity"].status == "unavailable"
    # No model server is named, so the models step is optional on the customer path:
    # verify reports it skipped with the one bring-your-own-key line, not a required action.
    models = observations["models"]
    assert models.status == "skipped"
    assert "bring your own key" in models.evidence["skipped"]
    assert "workspace-repositories" not in observations
    assert "base-tools" not in observations
    assert all(not item.changed for item in journal.observations)
    assert _snapshot(tmp_path) == before


def _conflicted_shell_home(tmp_path: Path) -> tuple[Path, dict[str, str]]:
    """A fake HOME with one legacy profile line, an inherited legacy variable and a
    shadow `onex` on PATH, so the shell-safety step reports one conflict of each
    non-profile-ambiguous kind (OMN-26614)."""
    home = tmp_path / "home"
    home.mkdir()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (home / ".bashrc").write_text(
        "# my shell config\nexport KAFKA_BROKERS=kafka:9092\n"
    )
    shadow = tmp_path / "shadow"
    shadow.mkdir()
    (shadow / "onex").write_text("#!/bin/sh\nexit 0\n")
    os.chmod(shadow / "onex", 0o755)
    cli = str(Path(sys.executable).parent / "onex")
    environment = {
        "HOME": str(home),
        "PATH": str(shadow) + os.pathsep + str(Path(cli).parent) + ":/usr/bin:/bin",
        "ONEX_STATE_DIR": str(tmp_path / "state"),
        "OMNI_HOME": str(tmp_path / "inherited-elsewhere"),
    }
    return home, environment


def test_shell_conflicts_print_one_plain_line_per_conflict(
    tmp_path: Path,
) -> None:
    """OMN-26614 AC1 and AC2: the human setup output (no --json) names each shell
    conflict in plain words, one line per conflict, and each line says whether
    --set-aside fixes it or it must be fixed by hand; the raw JSON blob the
    shell-safety step stores is never printed.

    The fixture uses an old KAFKA_BROKERS profile line instead of the PATH line the
    ticket names, because setup does not flag plain PATH lines: PATH is not a legacy
    variable in shell_policy.yaml, so a bare `export PATH=...` is never a conflict,
    while `^KAFKA_[A-Z_]+$` is. The PATH case the ticket's wording points at is the
    executable kind, fired here by a shadow `onex` earlier on PATH.
    """
    _, environment = _conflicted_shell_home(tmp_path)
    result = subprocess.run(
        (
            str(Path(sys.executable).parent / "onex"),
            "setup",
            "--action",
            "verify",
            "--component",
            "delegation",
            "--workspace",
            str(tmp_path / "workspace"),
            "--no-interactive",
        ),
        env=environment,
        cwd=tmp_path / "workspace",
        capture_output=True,
        text=True,
        input="",
        timeout=45,
        check=False,
    )
    assert result.returncode == 1, result.stdout + result.stderr
    out = result.stdout
    assert "conflicts: [{" not in out, out + result.stderr
    assert ".bashrc line 2: KAFKA_BROKERS is set by this profile line" in out
    assert (
        "current environment: OMNI_HOME is inherited from the current environment"
        in out
    )
    assert "PATH: another onex executable resolves first on PATH" in out
    assert "--set-aside preserves and disables it when setup runs again" in out, (
        out + result.stderr
    )
    assert out.count("resolve it by hand") >= 2, out + result.stderr


def test_json_output_keeps_structured_conflict_list(tmp_path: Path) -> None:
    """OMN-26614 AC3: the structured conflict list is carried by the --json output,
    and only there: the same run without --json prints plain words, not that list."""
    _, environment = _conflicted_shell_home(tmp_path)
    command = (
        str(Path(sys.executable).parent / "onex"),
        "setup",
        "--action",
        "verify",
        "--component",
        "delegation",
        "--workspace",
        str(tmp_path / "workspace"),
        "--no-interactive",
    )
    result = subprocess.run(
        (*command, "--json"),
        env=environment,
        cwd=tmp_path / "workspace",
        capture_output=True,
        text=True,
        input="",
        timeout=45,
        check=False,
    )
    assert result.stdout.startswith("{"), result.stdout + result.stderr
    journal = ModelSetupJournal.model_validate_json(result.stdout)
    shell = next(item for item in journal.observations if item.step == "shell-safety")
    conflicts = json.loads(shell.evidence["conflicts"])
    by_kind = {(row["kind"], row["name"]): row for row in conflicts}
    profile = by_kind[("profile", "KAFKA_BROKERS")]
    assert (profile["location"], profile["line"], profile["recoverable"]) == (
        ".bashrc",
        2,
        True,
    )
    assert ("environment", "OMNI_HOME") in by_kind
    assert ("executable", "onex") in by_kind

    human = subprocess.run(
        command,
        env=environment,
        cwd=tmp_path / "workspace",
        capture_output=True,
        text=True,
        input="",
        timeout=45,
        check=False,
    )
    assert '"kind"' not in human.stdout, human.stdout + human.stderr
    assert ".bashrc line 2: KAFKA_BROKERS" in human.stdout, human.stdout


def test_secret_bearing_plan_is_refused_before_writes(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    plan = tmp_path / "bad.json"
    plan.write_text(
        json.dumps(
            {
                "workspace": str(tmp_path / "workspace"),
                "providers": [
                    {
                        "provider": "openai",
                        "models": ["model"],
                        "api_key": "credential-SENTINEL",
                    }
                ],
            }
        )
    )
    before = _snapshot(tmp_path)
    result = subprocess.run(
        [
            str(Path(sys.executable).parent / "onex"),
            "setup",
            "--plan",
            str(plan),
            "--json",
            "--no-interactive",
        ],
        env={
            "HOME": str(home),
            "PATH": str(Path(sys.executable).parent)
            + os.pathsep
            + os.environ.get("PATH", "/usr/bin:/bin"),
        },
        cwd=tmp_path,
        capture_output=True,
        text=True,
        input="",
        timeout=30,
    )
    assert result.returncode != 0
    assert "SENTINEL" not in result.stdout + result.stderr
    assert _snapshot(tmp_path) == before


def test_default_setup_uses_the_same_versioned_engine(tmp_path: Path) -> None:
    """A conflicting workspace stops before inference, through the default path."""
    home = tmp_path / "home"
    home.mkdir()
    workspace = tmp_path / "user file with spaces"
    workspace.write_text("user-owned data\n")
    result = subprocess.run(
        [
            str(Path(sys.executable).parent / "onex"),
            "setup",
            "--json",
            "--yes",
            "--no-interactive",
            "--workspace",
            str(workspace),
            "--provider",
            "local",
            "--model",
            "fixture-never-called",
            "--url",
            "http://127.0.0.1:9",
        ],
        env={
            "HOME": str(home),
            "PATH": str(Path(sys.executable).parent)
            + os.pathsep
            + os.environ.get("PATH", "/usr/bin:/bin"),
            "ONEX_STATE_DIR": str(tmp_path / "state"),
        },
        cwd=tmp_path,
        capture_output=True,
        text=True,
        input="",
        timeout=30,
    )
    assert result.returncode != 0
    journal = ModelSetupJournal.model_validate_json(result.stdout)
    assert journal.schema_version == 2
    assert journal.observations[0].step == "preflight"
    assert journal.observations[-1].status in (
        "action_required",
        "unavailable",
        "failure",
    )
    assert not any(item.step == "models" for item in journal.observations)
    saved = ModelSetupJournal.model_validate_json(
        (home / ".onex" / "setup.json").read_bytes()
    )
    assert saved == journal
    assert workspace.read_text() == "user-owned data\n"
