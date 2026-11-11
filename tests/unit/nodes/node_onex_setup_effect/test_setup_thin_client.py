# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Thin-client failure controls, defined before retiring lab prerequisites.

A remote endpoint must work on a small, unprivileged client without developer
tools. Developer selection and imported state must never acquire repositories.
The controls stop at model configuration; they do not claim inference proof.
"""

from pathlib import Path

import pytest

from omnimarket.nodes.node_onex_setup_effect.handlers.handler_setup_command import (
    HandlerSetupCommand,
)
from omnimarket.nodes.node_onex_setup_effect.handlers.handler_setup_preflight import (
    HandlerSetupPreflight,
)
from omnimarket.nodes.node_onex_setup_effect.handlers.handler_setup_workflow import (
    HandlerSetupWorkflow,
)
from omnimarket.nodes.node_onex_setup_effect.models.model_setup_machine_facts import (
    ModelSetupMachineFacts,
)
from omnimarket.nodes.node_onex_setup_effect.models.model_setup_plan import (
    ModelSetupPlan,
)
from tests.helpers.setup_shell_environment import isolate_setup_shell

pytestmark = pytest.mark.unit


def small_client() -> ModelSetupMachineFacts:
    return ModelSetupMachineFacts(
        platform="Darwin",
        architecture="arm64",
        macos_version="14.0",
        cpus=1,
        ram_bytes=1024**3,
        free_disk_bytes=512 * 1024**2,
        administrator=False,
        virtual_machine=True,
        rosetta=False,
        missing_tools=("git", "gh", "brew", "jq", "uv", "xcode-command-line-tools"),
    )


def test_small_unprivileged_endpoint_client_passes_preflight(tmp_path: Path) -> None:
    outcome = HandlerSetupPreflight(collect=lambda _: small_client()).handle(
        ModelSetupPlan(workspace=tmp_path)
    )
    assert outcome.status == "success", outcome.fix
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("developer", [False, True])
def test_default_workflow_reaches_model_configuration_without_source_acquisition(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, developer: bool
) -> None:
    from omnimarket.nodes.node_onex_setup_effect.handlers import (
        handler_setup_preflight,
    )

    monkeypatch.setattr(
        handler_setup_preflight, "collect_machine_facts", lambda _: small_client()
    )
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("ONEX_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.delenv("OMNIBASE_PATH", raising=False)
    isolate_setup_shell(monkeypatch)
    commands: list[tuple[str, ...]] = []

    def refuse_command(_self: object, request: object) -> None:
        commands.append(request.argv)
        raise AssertionError("Endpoint setup attempted a provisioning subprocess")

    monkeypatch.setattr(HandlerSetupCommand, "handle", refuse_command)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / ".onex-setup-workspace").write_text("onex setup\n")
    sentinel = workspace / "contributor-work.txt"
    sentinel.write_text("keep my work")
    journal = HandlerSetupWorkflow(onex_home=tmp_path / "config").handle(
        ModelSetupPlan(workspace=workspace, developer=developer)
    )
    observations = {item.step: item for item in journal.observations}
    assert "models" in observations, journal.model_dump_json()
    assert observations["models"].status == "action_required"
    assert commands == []
    assert sentinel.read_text() == "keep my work"
    assert not (workspace / "omnimarket").exists()
