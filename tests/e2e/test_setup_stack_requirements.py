# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Selected Docker requirements via controlled process transport, never Docker.

Catch a running-but-unhealthy stack, declared reservations above engine capacity,
terms/permission refusal, an omitted selected service, and secret-bearing process
errors. Tiny stacks below old universal floors remain allowed. Artifact records
only commands and typed outcomes, never the injected environment/diagnostic.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from omnimarket.nodes.node_onex_setup_effect.handlers.handler_setup_command import (
    HandlerSetupCommand,
)
from omnimarket.nodes.node_onex_setup_effect.models.model_setup_command_result import (
    ModelSetupCommandResult,
)
from omnimarket.nodes.node_onex_setup_effect.models.model_setup_plan import (
    ModelSetupPlan,
)

pytestmark = pytest.mark.e2e


def _plan(tmp_path: Path) -> ModelSetupPlan:
    compose = tmp_path / "compose.yaml"
    compose.write_text("services: {worker: {image: user-selected}}\n")
    topology = tmp_path / "topology.yaml"
    topology.write_text(
        "schema_version: 1.0.0\nservices:\n  worker:\n    mode: LOCAL\n    local: {compose_service: worker, host_port: 38123}\n"
    )
    return ModelSetupPlan(
        workspace=tmp_path / "workspace",
        components=("local-stack",),
        local_stack={"compose_file": compose, "topology_file": topology},
    )


@pytest.mark.parametrize(
    ("case", "expected"),
    [
        ("tiny", "success"),
        ("memory", "action_required"),
        ("cpu", "action_required"),
        ("terms", "action_required"),
        ("unhealthy", "unavailable"),
        ("missing", "unavailable"),
        ("bad-json", "failure"),
    ],
)
def test_selected_stack_requirements_and_health(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, case: str, expected: str
) -> None:
    from omnimarket.nodes.node_onex_setup_effect.handlers.handler_setup_stack_requirements import (
        HandlerSetupStackRequirements,
    )

    commands = []
    sentinel = "planted-stack-private-diagnostic"
    info = {"NCPU": 1, "MemTotal": 256 * 1024 * 1024, "OSType": "linux"}
    services = {
        "worker": {
            "deploy": {
                "resources": {
                    "reservations": {"cpus": "0.1", "memory": 32 * 1024 * 1024}
                }
            },
            "environment": {"SECRET": sentinel},
            "healthcheck": {"test": ["CMD", "true"]},
        }
    }
    if case == "memory":
        services["worker"]["deploy"]["resources"]["reservations"]["memory"] = (
            512 * 1024 * 1024
        )
    if case == "cpu":
        services["worker"]["deploy"]["resources"]["reservations"]["cpus"] = "2"
    rows = (
        []
        if case == "missing"
        else [
            {
                "Service": "worker",
                "State": "running",
                "Health": "unhealthy" if case == "unhealthy" else "healthy",
            }
        ]
    )

    def transport(self, request):
        commands.append(list(request.argv))
        if "info" in request.argv:
            if case == "terms":
                return ModelSetupCommandResult(
                    argv=request.argv,
                    returncode=1,
                    stdout="",
                    stderr="accept terms " + sentinel,
                )
            raw = "bad " + sentinel if case == "bad-json" else json.dumps(info)
        elif "config" in request.argv:
            raw = json.dumps({"services": services})
        elif "ps" in request.argv:
            raw = "\n".join(json.dumps(row) for row in rows)
        else:
            raise AssertionError(request.argv)
        return ModelSetupCommandResult(
            argv=request.argv, returncode=0, stdout=raw, stderr=""
        )

    monkeypatch.setattr(HandlerSetupCommand, "handle", transport)
    from omnimarket.nodes.node_onex_setup_effect.models.model_setup_stack_requirements import (
        ModelSetupStackRequirementsRequest,
    )

    result = HandlerSetupStackRequirements().handle(
        ModelSetupStackRequirementsRequest(plan=_plan(tmp_path), services=("worker",))
    )
    assert result.status == expected
    from uuid import uuid4

    from omnibase_infra.nodes.node_setup_local_provision_effect.models.model_local_provision_effect_output import (
        ModelLocalProvisionEffectOutput,
    )

    from omnimarket.nodes.node_onex_setup_effect.handlers.handler_setup_local_stack import (
        HandlerSetupLocalStack,
    )

    async def running(_handler, _envelope):
        return ModelLocalProvisionEffectOutput(
            success=True, correlation_id=uuid4(), services_running=("worker",)
        )

    owner = HandlerSetupLocalStack(onex_home=tmp_path / "onex")
    plan = _plan(tmp_path)
    owner._applied_digest = owner._inputs(plan)[2]
    monkeypatch.setattr(owner, "_execute", running)
    monkeypatch.setattr("shutil.which", lambda _name: "/fixture/docker")
    integrated = owner.observe(plan)
    assert integrated.status == expected, (
        "running services must not hide resource or health failures"
    )
    assert sentinel not in result.model_dump_json()
    assert not any("up" in cmd or "restart" in cmd for cmd in commands)
    assert not (tmp_path / "workspace").exists()
    if case == "tiny":
        assert result.evidence["resource_basis"] == "selected service reservations only"
    (tmp_path / "stack-requirements-evidence.json").write_text(
        json.dumps(
            {
                "proof_kind": "injected process transport; no Docker engine",
                "commands": commands,
                "result": result.model_dump(mode="json"),
            },
            indent=2,
        )
    )
