# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The models step is optional when no local model server is configured (OMN-26317).

A customer on the cloud route brings their own key and runs no local model server, so an
unattended setup that names no model must finish on the steps it can complete and say, once,
how to add a local model later. A server that IS named but does not answer keeps the hard
failure, and the typed failure names the server and the cause.

The model step is replaced through the existing seam, so no test here opens a connection.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner, Result
from omnibase_core.cli.cli_commands import cli

from omnimarket.nodes.node_onex_setup_effect.handlers import handler_onex_setup
from omnimarket.nodes.node_onex_setup_effect.handlers.handler_setup_workflow import (
    HandlerSetupWorkflow,
)
from omnimarket.nodes.node_onex_setup_effect.models.model_onex_setup import (
    ModelOnexSetupReceipt,
    ModelOnexSetupRequest,
)
from omnimarket.nodes.node_onex_setup_effect.models.model_setup_plan import (
    ModelSetupPlan,
)

# Fixtures `home`, `recorder` and `server` come from this folder's conftest.
UNREACHABLE_URL = "http://127.0.0.1:9"
CAUSE = "the test delegation did not answer on local/placeholder: connection refused"


def _setup(workspace: Path, *flags: str) -> Result:
    return CliRunner().invoke(
        cli,
        ["setup", "--yes", "--no-interactive", "--workspace", str(workspace), *flags],
        input="",
        env={
            "OMNIBASE_PATH": None,
            "OMNI_HOME": None,
            # `onex setup` is listed only on the operator surface since the omnibase_infra pin raise.
            "ONEX_CLI_SHOW_ALL_COMMANDS": "1",
        },
    )


def _skip_lines(output: str) -> list[str]:
    return [line for line in output.splitlines() if "no local model server" in line]


@pytest.mark.parametrize("component", [[], ["--component", "delegation"]])
def test_models_optional_no_model_server_configured_exits_zero_with_one_skip_line(
    home: Path, recorder: object, tmp_path: Path, component: list[str]
) -> None:
    result = _setup(tmp_path / "ws", *component)

    assert result.exit_code == 0, result.output
    lines = _skip_lines(result.output)
    assert len(lines) == 1, result.output
    line = lines[0]
    assert "bring your own key" in line
    assert "onex models add local --url <base url> --model <name>" in line
    assert "failure" not in result.output.lower()


def test_models_optional_json_reports_the_models_step_as_skipped(
    home: Path, recorder: object, tmp_path: Path
) -> None:
    result = _setup(tmp_path / "ws", "--json")

    assert result.exit_code == 0, result.output
    journal = json.loads(result.stdout)
    statuses = {item["step"]: item["status"] for item in journal["observations"]}
    assert statuses["models"] == "skipped"
    assert {step for step, status in statuses.items() if status != "success"} == {
        "models"
    }


def test_models_optional_does_not_make_another_step_optional(
    home: Path, recorder: object, tmp_path: Path
) -> None:
    workspace = tmp_path / "ws"
    workspace.write_text("keep me\n")

    result = _setup(workspace, "--json")

    assert result.exit_code == 1, result.output
    statuses = {
        item["step"]: item["status"]
        for item in json.loads(result.stdout)["observations"]
    }
    assert statuses["workspace"] != "success"
    assert "models" not in statuses


def _unreachable_server(request: ModelOnexSetupRequest) -> ModelOnexSetupReceipt:
    raise handler_onex_setup.StepFailedError(
        "model",
        CAUSE,
        "fix the key or the server, then run the retry command again",
    )


def test_models_configured_unreachable_server_fails_with_typed_failure_in_the_cli(
    home: Path, recorder: object, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(handler_onex_setup, "prove_model", _unreachable_server)

    result = _setup(
        tmp_path / "ws",
        "--provider",
        "local",
        "--model",
        "placeholder",
        "--url",
        UNREACHABLE_URL,
        "--json",
    )

    assert result.exit_code == 1, result.output
    models = next(
        item
        for item in json.loads(result.stdout)["observations"]
        if item["step"] == "models"
    )
    assert models["status"] == "failure"
    assert models["evidence"]["server"] == UNREACHABLE_URL
    assert models["evidence"]["cause"] == CAUSE
    assert "skipped" not in result.output


def test_models_configured_unreachable_server_fails_the_workflow_through_the_seam(
    home: Path, tmp_path: Path
) -> None:
    plan = ModelSetupPlan.model_validate(
        {
            "workspace": tmp_path / "ws",
            "providers": [
                {
                    "provider": "local",
                    "models": ["placeholder"],
                    "url": UNREACHABLE_URL,
                }
            ],
        }
    )

    journal = HandlerSetupWorkflow(
        onex_home=home / ".onex", model_step=_unreachable_server
    ).handle(plan)

    models = journal.observations[-1]
    assert models.step == "models"
    assert models.status == "failure"
    assert models.evidence == {
        "provider": "local",
        "server": UNREACHABLE_URL,
        "cause": CAUSE,
    }
    assert models.fix == "fix the key or the server, then run the retry command again"


def test_models_optional_skipped_step_is_never_named_as_the_one_that_stopped_setup() -> (
    None
):
    from omnimarket.nodes.node_onex_setup_effect.cli_tui import _stopped_by

    document = {
        "observations": [
            {"step": "models", "status": "skipped", "fix": None},
            {"step": "claude-plugin", "status": "failure", "fix": "Sign in first."},
        ]
    }

    assert _stopped_by(json.dumps(document), "") == "claude-plugin — Sign in first."


def test_models_configured_unreachable_failure_never_echoes_the_supplied_key(
    home: Path, tmp_path: Path
) -> None:
    from pydantic import SecretStr

    sentinel = "sk-sentinel-0123456789"

    def leaks(request: ModelOnexSetupRequest) -> ModelOnexSetupReceipt:
        raise handler_onex_setup.StepFailedError(
            "model", f"rejected {sentinel} at the server", "check the key"
        )

    plan = ModelSetupPlan.model_validate(
        {
            "workspace": tmp_path / "ws",
            "providers": [
                {
                    "provider": "openai_compatible",
                    "models": ["m"],
                    "url": "https://models.example",
                    "api_key_required": True,
                }
            ],
        }
    )

    journal = HandlerSetupWorkflow(
        onex_home=home / ".onex",
        credentials={"openai_compatible": SecretStr(sentinel)},
        model_step=leaks,
    ).handle(plan)

    models = journal.observations[-1]
    assert models.status == "failure"
    assert sentinel not in journal.model_dump_json()
    assert models.evidence["server"] == "https://models.example"
