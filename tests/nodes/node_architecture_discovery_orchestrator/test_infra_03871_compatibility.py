"""OMN-17427: release pin and discovery's workspace-validation wire contract."""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from packaging.requirements import Requirement
from packaging.version import Version

REPO = Path(__file__).resolve().parents[3]


def test_infra_release_floor_and_lock_include_vendored_migrations() -> None:
    project = tomllib.loads((REPO / "pyproject.toml").read_text())
    lock = tomllib.loads((REPO / "uv.lock").read_text())
    package = next(p for p in lock["package"] if p["name"] == "omnibase-infra")
    locked_version = Version(package["version"])
    assert locked_version >= Version("0.38.71")
    for dependencies in (
        project["project"]["dependencies"],
        project["tool"]["uv"]["override-dependencies"],
    ):
        requirement = next(
            Requirement(value)
            for value in dependencies
            if Requirement(value).name == "omnibase-infra"
        )
        floors = [s for s in requirement.specifier if s.operator in {">=", ">"}]
        assert floors
        assert all(Version(s.version) >= Version("0.38.71") for s in floors)
        assert locked_version in requirement.specifier
        assert Version("0.38.70") not in requirement.specifier


@pytest.mark.parametrize(
    ("floor", "locked", "accepted"),
    [
        ("0.38.71", "0.38.71", True),
        ("0.38.72", "0.38.72", True),
        ("0.38.73", "0.38.73", True),
        ("0.38.70", "0.38.72", False),
        ("0.38.70.1", "0.38.72", False),
        ("0.38.72", "0.38.71", False),
        ("0.38.71", "0.39.0", False),
    ],
)
def test_infra_release_floor_accepts_newer_releases_and_refuses_incompatible_pins(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    floor: str,
    locked: str,
    accepted: bool,
) -> None:
    import sys

    dependency = f'"omnibase-infra>={floor},<0.39.0"'
    (tmp_path / "pyproject.toml").write_text(
        f"[project]\ndependencies = [{dependency}]\n"
        f"[tool.uv]\noverride-dependencies = [{dependency}]\n"
    )
    (tmp_path / "uv.lock").write_text(
        f'[[package]]\nname = "omnibase-infra"\nversion = "{locked}"\n'
    )
    monkeypatch.setattr(sys.modules[__name__], "REPO", tmp_path)
    if accepted:
        test_infra_release_floor_and_lock_include_vendored_migrations()
    else:
        with pytest.raises(AssertionError):
            test_infra_release_floor_and_lock_include_vendored_migrations()


def test_workspace_validation_carries_invoke_command_before_agent_runs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from omnibase_infra.models.coding_agent import ModelWorkspaceValidateCommand

    from omnimarket.nodes.node_architecture_discovery_orchestrator.adapters import (
        handler_coding_agent_bus,
    )
    from omnimarket.nodes.node_architecture_discovery_orchestrator.models import (
        ModelDiscoveryTask,
    )

    task = ModelDiscoveryTask(
        label="discovery-linear",
        phase="Scan",
        model="sonnet",
        effort="high",
        prompt="Inspect the workspace",
        schema_definition={},
        workspace_path="/allowed/requested",
        correlation_id=uuid4(),
        timeout_ms=1000,
    )
    calls: list[str] = []

    def publish(**kwargs: Any) -> dict[str, object]:
        calls.append(kwargs["node_id"])
        assert kwargs["node_id"] == "node_coding_agent_workspace_compute"
        payload = dict(kwargs["payload"])
        invoke = payload["invoke_command"]
        payload["correlation_id"] = str(task.correlation_id)
        command = ModelWorkspaceValidateCommand.model_validate(payload)
        assert invoke["correlation_id"] == str(task.correlation_id)
        assert invoke["workspace_path"] == task.workspace_path
        assert invoke["model"] == task.model
        assert invoke["timeout_ms"] == task.timeout_ms
        assert invoke["prompt"].startswith(task.prompt)
        assert command.workspace_path == task.workspace_path
        return {
            "payload": {
                "correlation_id": str(task.correlation_id),
                "valid": False,
                "resolved_path": task.workspace_path,
                "rejection_reason": "outside allowed roots",
                "invoke_command": invoke,
            }
        }

    monkeypatch.setenv("KAFKA_BOOTSTRAP_SERVERS", "fixture:9092")
    monkeypatch.setenv("ONEX_CODING_AGENT_WORKSPACE_ROOT", "/allowed")
    with pytest.raises(ValueError, match="workspace rejected"):
        handler_coding_agent_bus.HandlerCodingAgentBus(publish=publish).execute(task)
    assert calls == ["node_coding_agent_workspace_compute"]
