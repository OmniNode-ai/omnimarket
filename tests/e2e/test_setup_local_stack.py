# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Isolated owner integration controls; these do not prove Docker containers.

Failure modes: missing explicit input, missing Docker, partial running services,
changed configuration hidden by healthy status, failed startup, secret-bearing
owner diagnostics, and no-op rerun writing or provisioning again. The installed
infra owners execute, with only their process/port transport injected. No Docker
or Postgres process is started on the developer Mac.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from threading import Thread

import pytest

from omnimarket.nodes.node_onex_setup_effect.handlers.handler_setup_plugins import (
    HandlerSetupPlugins,
)
from omnimarket.nodes.node_onex_setup_effect.handlers.handler_setup_workflow import (
    HandlerSetupWorkflow,
)
from omnimarket.nodes.node_onex_setup_effect.models.model_setup_plan import (
    ModelSetupPlan,
)


def _plan(tmp_path: Path) -> ModelSetupPlan:
    compose = tmp_path / "compose.yaml"
    compose.write_text("name: explicitly-supplied-stack\nservices: {}\n")
    topology = tmp_path / "topology.json"
    topology.write_text(
        json.dumps(
            {
                "schema_version": "1.0.0",
                "services": {
                    "worker": {
                        "mode": "LOCAL",
                        "local": {
                            "compose_service": "chosen-worker",
                            "host_port": 43129,
                        },
                    },
                },
            }
        )
    )
    return ModelSetupPlan.model_validate(
        {
            "workspace": tmp_path / "workspace",
            "components": ["local-stack"],
            "local_stack": {"compose_file": compose, "topology_file": topology},
        }
    )


def test_local_stack_is_explicit_and_registered() -> None:
    plugins = HandlerSetupPlugins()
    assert "local-stack" in plugins.components()
    assert "local-stack" not in plugins.default_components()


def test_stack_reconciliation_uses_owner_and_detects_changed_configuration(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    from omnibase_infra.nodes.node_setup_local_provision_effect.handlers import (
        handler_local_provision as provision_module,
    )
    from omnibase_infra.nodes.node_setup_local_provision_effect.handlers import (
        handler_local_status as status_module,
    )

    from omnimarket.nodes.node_onex_setup_effect.handlers.handler_setup_local_stack import (
        HandlerSetupLocalStack,
    )

    commands: list[list[str]] = []
    running: list[str] = ["unrelated-service"]
    refuse = False
    original_filters = tuple(logging.getLogger(provision_module.__name__).filters)

    async def transport(cmd: list[str]) -> tuple[int, str, str]:
        commands.append(cmd)
        if "ps" in cmd:
            return 0, "", "\n".join(running)
        assert cmd[-3:] == ["up", "-d", "chosen-worker"]
        if refuse:
            logging.getLogger(provision_module.__name__).error(
                "owner details %s",
                "planted-secret-do-not-report",
                extra={"payload": "planted-secret-do-not-report"},
            )
            # The same owner's concurrent, unrelated invocation must keep its log.
            concurrent = Thread(
                target=lambda: logging.getLogger(provision_module.__name__).warning(
                    "unrelated-owner-diagnostic"
                )
            )
            concurrent.start()
            concurrent.join()
            return 1, "planted-secret-do-not-report", ""
        running.append("chosen-worker")
        return 0, "", ""

    async def port_ready(host: str, port: int, max_wait: float) -> bool:
        assert port == 43129
        return True

    from omnimarket.nodes.node_onex_setup_effect.handlers.handler_setup_command import (
        HandlerSetupCommand,
    )
    from omnimarket.nodes.node_onex_setup_effect.models.model_setup_command_result import (
        ModelSetupCommandResult,
    )

    def requirements_transport(self, request):
        if "info" in request.argv:
            body = {"NCPU": 1, "MemTotal": 256 * 1024 * 1024}
        elif "config" in request.argv:
            body = {"services": {"chosen-worker": {}}}
        elif "ps" in request.argv:
            body = [
                {"Service": name, "State": "running", "Health": ""} for name in running
            ]
        else:
            raise AssertionError(request.argv)
        return ModelSetupCommandResult(
            argv=request.argv, returncode=0, stdout=json.dumps(body), stderr=""
        )

    monkeypatch.setattr(HandlerSetupCommand, "handle", requirements_transport)
    monkeypatch.setattr(provision_module, "run_compose", transport)
    monkeypatch.setattr(status_module, "run_compose", transport)
    monkeypatch.setattr(provision_module, "_poll_port_open", port_ready)
    monkeypatch.setattr("shutil.which", lambda _name: "/fixture/transport-only")
    plan = _plan(tmp_path)
    home = tmp_path / "onex"

    def reconcile() -> object:
        return HandlerSetupWorkflow(
            onex_home=home,
            steps=(HandlerSetupLocalStack(onex_home=home),),
        ).handle(plan)

    first = reconcile()
    assert first.observations[-1].status == "success"
    assert first.observations[-1].changed
    assert len([cmd for cmd in commands if "up" in cmd]) == 1
    snapshot = (home / "setup.json").read_bytes()
    second = reconcile()
    assert second.observations[-1].status == "success"
    assert not second.observations[-1].changed
    assert (home / "setup.json").read_bytes() == snapshot
    assert len([cmd for cmd in commands if "up" in cmd]) == 1

    assert plan.local_stack is not None
    plan.local_stack.compose_file.write_text(
        "name: explicitly-supplied-stack\nservices: {}\n# edited\n"
    )
    third = reconcile()
    assert third.observations[-1].status == "success"
    assert len([cmd for cmd in commands if "up" in cmd]) == 2
    assert "unrelated-service" in running
    running.remove("chosen-worker")
    running[:] = [name for name in running if name != "chosen-worker"]
    refuse = True
    failure = reconcile()
    assert failure.observations[-1].status == "failure"
    assert "planted-secret-do-not-report" not in failure.model_dump_json()
    assert "planted-secret-do-not-report" not in caplog.text
    assert "planted-secret-do-not-report" not in repr(
        [record.__dict__ for record in caplog.records]
    )
    assert "unrelated-owner-diagnostic" in caplog.text
    assert (
        tuple(logging.getLogger(provision_module.__name__).filters) == original_filters
    )
    assert not any("down" in cmd or "build" in cmd for cmd in commands)
    (tmp_path / "stack-isolated-proof.json").write_text(
        json.dumps(
            {
                "proof_kind": "injected transport; no actual containers",
                "commands": commands,
                "outcomes": [
                    first.model_dump(mode="json"),
                    second.model_dump(mode="json"),
                    third.model_dump(mode="json"),
                    failure.model_dump(mode="json"),
                ],
            },
            indent=2,
        )
    )


def test_stack_missing_inputs_and_dependency_refuse_without_processes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from omnimarket.nodes.node_onex_setup_effect.handlers.handler_setup_local_stack import (
        HandlerSetupLocalStack,
    )

    owner = HandlerSetupLocalStack(onex_home=tmp_path / "onex")
    empty = ModelSetupPlan(workspace=tmp_path, components=("local-stack",))
    assert owner.observe(empty).status == "action_required"
    assert "compose" in owner.observe(empty).fix.lower()
    plan = _plan(tmp_path)
    monkeypatch.setattr("shutil.which", lambda _name: None)
    refused = owner.observe(plan)
    assert refused.status == "action_required"
    assert "Docker" in refused.fix
    assert not (tmp_path / "onex").exists()
