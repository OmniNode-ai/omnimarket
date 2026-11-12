# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Subprocess failure controls defined before the setup command effect.

Catch key exposure, shell interpolation, inherited workspace/tenant pollution,
missing executables and timeouts through actual short Python children.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from pydantic import SecretStr, ValidationError

from omnimarket.nodes.node_onex_setup_effect.handlers.handler_setup_command import (
    HandlerSetupCommand,
)
from omnimarket.nodes.node_onex_setup_effect.models.model_setup_command_request import (
    ModelSetupCommandRequest,
)

pytestmark = pytest.mark.unit


def test_shell_metacharacters_and_spaces_stay_literal(tmp_path: Path) -> None:
    marker = tmp_path / "must not exist"
    literal = f"$(touch '{marker}')"
    result = HandlerSetupCommand().handle(
        ModelSetupCommandRequest(
            argv=(sys.executable, "-c", "import sys; print(sys.argv[1])", literal),
            cwd=tmp_path,
        )
    )
    assert result.returncode == 0
    assert result.stdout.strip() == literal
    assert not marker.exists()


def test_secrets_on_stdin_are_redacted_from_both_streams() -> None:
    key = "SENTINEL_SETUP_SECRET_829"
    handler = HandlerSetupCommand(secrets=(SecretStr(key[:-3]), SecretStr(key)))
    request = ModelSetupCommandRequest(
        argv=(
            sys.executable,
            "-c",
            "import sys; s=sys.stdin.read(); print(s); print(s,file=sys.stderr)",
        ),
        stdin=SecretStr(key),
    )
    result = handler.handle(request)
    assert result.returncode == 0
    assert key not in result.model_dump_json()
    assert "[redacted]" in result.stdout
    assert "[redacted]" in result.stderr
    assert key not in request.model_dump_json()
    assert key not in repr(request)


def test_key_in_argv_is_refused_before_launch(tmp_path: Path) -> None:
    key = "SENTINEL_SETUP_SECRET_829"
    marker = tmp_path / "no-launch"
    request = ModelSetupCommandRequest(
        argv=(
            sys.executable,
            "-c",
            "from pathlib import Path; import sys; Path(sys.argv[1]).touch()",
            str(marker),
            key,
        )
    )
    with pytest.raises(ValueError, match="secret") as caught:
        HandlerSetupCommand(secrets=(SecretStr(key),)).handle(request)
    assert key not in str(caught.value)
    assert not marker.exists()


def test_workspace_and_tenant_environment_cannot_bleed_into_setup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    names = (
        "PYTHONPATH",
        "OMNI_HOME",
        "OMNIBASE_PATH",
        "ONEX_CONTRACTS_DIR",
        "ONEX_CONFIG_DIR",
        "ONEX_TENANT_ID",
        "ONEX_TENANT_SLUG",
        "GIT_DIR",
        "GIT_WORK_TREE",
        "GIT_INDEX_FILE",
        "GIT_COMMON_DIR",
        "GIT_OBJECT_DIRECTORY",
        "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    )
    for name in names:
        monkeypatch.setenv(name, "INHERITED_SENTINEL")
    result = HandlerSetupCommand().handle(
        ModelSetupCommandRequest(
            argv=(
                sys.executable,
                "-c",
                "import os,json; print(json.dumps({k:os.environ.get(k) for k in "
                + repr(names)
                + "}))",
            ),
            cwd=tmp_path,
            environment={"OMNIBASE_PATH": "/explicit/workspace with spaces"},
        )
    )
    assert result.returncode == 0
    values = json.loads(result.stdout)
    assert values.pop("OMNIBASE_PATH") == "/explicit/workspace with spaces"
    assert all(v is None for v in values.values())


def test_missing_executable_is_a_typed_failure(tmp_path: Path) -> None:
    result = HandlerSetupCommand().handle(
        ModelSetupCommandRequest(argv=(str(tmp_path / "absent"),))
    )
    assert result.returncode == 127
    assert not result.timed_out
    assert "Traceback" not in result.stderr


def test_timeout_preserves_only_redacted_partial_output() -> None:
    key = "SENTINEL_SETUP_SECRET_829"
    result = HandlerSetupCommand(secrets=(SecretStr(key),)).handle(
        ModelSetupCommandRequest(
            argv=(
                sys.executable,
                "-c",
                "import sys,time; print(sys.stdin.read(),flush=True); time.sleep(3)",
            ),
            stdin=SecretStr(key),
            timeout_seconds=0.25,
        )
    )
    assert result.returncode == 124
    assert result.timed_out
    assert key not in result.model_dump_json()
    assert "[redacted]" in result.stdout


@pytest.mark.parametrize(
    "fields",
    [
        {"argv": ()},
        {"argv": ("x",), "timeout_seconds": 0},
        {"argv": ("x",), "unknown_field": "refuse"},
    ],
)
def test_invalid_commands_fail_before_a_process_is_started(
    fields: dict[str, object],
) -> None:
    with pytest.raises(ValidationError):
        ModelSetupCommandRequest.model_validate(fields)
