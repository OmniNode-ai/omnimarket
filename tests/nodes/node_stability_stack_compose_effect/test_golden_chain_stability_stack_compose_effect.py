# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden chain, error chain and failing control of node_stability_stack_compose_effect (OMN-23968).

A fake runner records every argv and answers from a script; a fake prober answers a status. Synthetic values only.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from uuid import UUID

import pytest

from omnimarket.nodes.node_stability_stack_compose_effect.handlers.handler_stack_compose import (
    HandlerStackCompose,
)
from omnimarket.nodes.node_stability_stack_compose_effect.models.model_stack_compose import (
    EnumStackRefusalReason,
    EnumStackStepOutcome,
    ModelCommandOutput,
    ModelStackStepRequest,
)
from omnimarket.nodes.node_stability_stack_compose_effect.overlay import (
    ModelStackComposeOverlay,
)
from omnimarket.nodes.node_stability_stack_plan_compute.models.model_stack_plan import (
    EnumStackStepKind,
    ModelStackPlanOverlay,
    ModelStackStep,
)

CID = UUID("11111111-1111-1111-1111-111111111111")
COMPOSE_TEXT = "name: demo-base\nservices:\n  db:\n    container_name: demo-base-db\nvolumes:\n  demo-base-data: {}\n"


def _plan_overlay() -> ModelStackPlanOverlay:
    return ModelStackPlanOverlay.model_validate(
        {
            "tag_pattern": r"^dev-\d{14}-[0-9a-f]{12}$",
            "stacks": [
                {
                    "name": "demo-lane",
                    "host": "lab-box-a",
                    "compose_project": "demo-lane-project",
                    "source_dir_env": "DEMO_SOURCE_DIR",
                    "compose_files": ["compose/base.yml"],
                    "name_rewrites": [["demo-base", "demo-lane"]],
                    "env": [["DEMO_IMAGE_TAG", "dev-20260101000000-0123456789ab"]],
                    "ports": [["DEMO_DB_PORT", 61001]],
                    "container_names": ["demo-lane-db"],
                    "volumes": ["demo-lane-data"],
                    "release_tag": "dev-20260101000000-0123456789ab",
                    "tiers": [{"name": "infra", "services": ["db"]}],
                    "health_reads": [
                        {
                            "container": "demo-lane-db",
                            "port_key": "DEMO_DB_PORT",
                            "path": "/health",
                            "expect_status": 200,
                        }
                    ],
                    "event_roundtrip": {
                        "broker_container": "demo-lane-db",
                        "topic": "demo.topic",
                        "message": "ping",
                    },
                    "remove_volumes_on_down": True,
                }
            ],
            "reserved": [
                {
                    "name": "other",
                    "host": "lab-box-b",
                    "compose_project": "other-project",
                    "container_names": ["other-db"],
                    "volumes": ["other-data"],
                    "ports": [61002],
                }
            ],
        }
    )


def _overlay(local_host: str = "lab-box-a") -> ModelStackComposeOverlay:
    return ModelStackComposeOverlay(
        local_host=local_host,
        docker_binary="container-cli-example",
        render_dir_env="DEMO_RENDER_DIR",
        command_timeout_seconds=30,
        up_wait_seconds=20,
        health_timeout_seconds=5,
        event_timeout_seconds=10,
    )


class FakeRunner:
    def __init__(self, script: dict[str, ModelCommandOutput] | None = None) -> None:
        self.calls: list[tuple[str, ...]] = []
        self._script = script or {}

    def run(
        self, argv: tuple[str, ...], env: Mapping[str, str], timeout_seconds: int
    ) -> ModelCommandOutput:
        self.calls.append(argv)
        for needle, out in self._script.items():
            if needle in " ".join(argv):
                return out
        return ModelCommandOutput(returncode=0, stdout="", stderr="")


class FakeProber:
    def __init__(self, status: int) -> None:
        self._status = status

    def status(self, url: str, timeout_seconds: int) -> int:
        return self._status


def _ps(state: str = "running", health: str = "healthy") -> ModelCommandOutput:
    return ModelCommandOutput(
        returncode=0,
        stdout=json.dumps(
            [{"Name": "demo-lane-db", "State": state, "Health": health, "ExitCode": 0}]
        ),
        stderr="",
    )


def _handler(
    tmp_path: Path, runner: FakeRunner, status: int = 200, local_host: str = "lab-box-a"
) -> HandlerStackCompose:
    source = tmp_path / "src"
    (source / "compose").mkdir(parents=True, exist_ok=True)
    (source / "compose" / "base.yml").write_text(COMPOSE_TEXT, encoding="utf-8")
    environ = {
        "DEMO_SOURCE_DIR": str(source),
        "DEMO_RENDER_DIR": str(tmp_path / "render"),
    }
    return HandlerStackCompose(
        _plan_overlay(), _overlay(local_host), runner, FakeProber(status), environ
    )


def _req(
    kind: EnumStackStepKind, target: str = "", stack: str = "demo-lane"
) -> ModelStackStepRequest:
    return ModelStackStepRequest(
        correlation_id=CID,
        stack=stack,
        step=ModelStackStep(index=0, kind=kind, target=target),
    )


def test_golden_chain_every_step_runs_its_exact_argv(tmp_path: Path) -> None:
    runner = FakeRunner(
        {
            "ps -a --format json": _ps(),
            "rpk topic consume": ModelCommandOutput(
                returncode=0, stdout=f"ping-{CID}\n", stderr=""
            ),
        }
    )
    handler = _handler(tmp_path, runner)
    rendered = handler.handle(_req(EnumStackStepKind.RENDER))
    assert rendered.outcome == EnumStackStepOutcome.OK
    assert len(rendered.digest) == 64
    text = (tmp_path / "render" / "demo-lane-project" / "base.yml").read_text(
        encoding="utf-8"
    )
    assert "demo-lane-db" in text
    assert "demo-base" not in text
    base = (
        "container-cli-example",
        "compose",
        "-p",
        "demo-lane-project",
        "-f",
        str(tmp_path / "render" / "demo-lane-project" / "base.yml"),
    )
    steps = [
        (EnumStackStepKind.SNAPSHOT, "", (*base, "ps", "--format", "json")),
        (
            EnumStackStepKind.UP,
            "db",
            (*base, "up", "-d", "--wait", "--wait-timeout", "20", "db"),
        ),
        (EnumStackStepKind.READ_SERVICES, "", (*base, "ps", "-a", "--format", "json")),
        (EnumStackStepKind.DOWN, "", (*base, "down", "--remove-orphans", "-v")),
    ]
    for kind, target, argv in steps:
        result = handler.handle(_req(kind, target))
        assert result.outcome == EnumStackStepOutcome.OK, (kind, result.reading)
        assert result.digest == rendered.digest
        assert runner.calls[-1] == argv
    assert (
        handler.handle(_req(EnumStackStepKind.READ_HEALTH, "demo-lane-db")).outcome
        == EnumStackStepOutcome.OK
    )
    event = handler.handle(_req(EnumStackStepKind.EVENT_ROUNDTRIP, "demo-lane-db"))
    assert event.outcome == EnumStackStepOutcome.OK
    assert event.items == (f"ping-{CID}",)
    gone = handler.handle(_req(EnumStackStepKind.READ_GONE))
    assert gone.outcome == EnumStackStepOutcome.OK
    assert runner.calls[-1][:3] == ("container-cli-example", "volume", "ls")


def test_error_chain_unknown_stack_and_other_host_refuse_before_any_command(
    tmp_path: Path,
) -> None:
    runner = FakeRunner()
    unknown = _handler(tmp_path, runner).handle(
        _req(EnumStackStepKind.RENDER, stack="no-such-stack")
    )
    assert (unknown.outcome, unknown.reason) == (
        EnumStackStepOutcome.REFUSED,
        EnumStackRefusalReason.UNKNOWN_STACK,
    )
    other = _handler(tmp_path, runner, local_host="lab-box-z").handle(
        _req(EnumStackStepKind.UP, "db")
    )
    assert (other.outcome, other.reason) == (
        EnumStackStepOutcome.REFUSED,
        EnumStackRefusalReason.HOST_NOT_LOCAL,
    )
    assert runner.calls == []


def test_control_the_same_request_runs_on_the_declared_host(tmp_path: Path) -> None:
    runner = FakeRunner()
    handler = _handler(tmp_path, runner)
    handler.handle(_req(EnumStackStepKind.RENDER))
    assert (
        handler.handle(_req(EnumStackStepKind.UP, "db")).outcome
        == EnumStackStepOutcome.OK
    )
    assert len(runner.calls) == 1


def test_error_chain_not_rendered_source_unset_and_absent_name(tmp_path: Path) -> None:
    handler = _handler(tmp_path, FakeRunner())
    not_rendered = handler.handle(_req(EnumStackStepKind.UP, "db"))
    assert not_rendered.reason == EnumStackRefusalReason.NOT_RENDERED
    unset = HandlerStackCompose(
        _plan_overlay(),
        _overlay(),
        FakeRunner(),
        FakeProber(200),
        {"DEMO_RENDER_DIR": str(tmp_path)},
    )
    assert (
        unset.handle(_req(EnumStackStepKind.RENDER)).reason
        == EnumStackRefusalReason.SOURCE_DIR_UNSET
    )
    (tmp_path / "src" / "compose" / "base.yml").write_text(
        "name: demo-base\n", encoding="utf-8"
    )
    absent = handler.handle(_req(EnumStackStepKind.RENDER))
    assert absent.reason == EnumStackRefusalReason.DECLARED_NAME_ABSENT
    assert absent.reading == "demo-lane-db"


def test_error_chain_failed_commands_and_readings(tmp_path: Path) -> None:
    failing = FakeRunner(
        {
            "up -d": ModelCommandOutput(
                returncode=1, stdout="", stderr="port is already allocated"
            )
        }
    )
    handler = _handler(tmp_path, failing)
    handler.handle(_req(EnumStackStepKind.RENDER))
    up = handler.handle(_req(EnumStackStepKind.UP, "db"))
    assert up.outcome == EnumStackStepOutcome.FAILED
    assert "port is already allocated" in up.reading
    sick = _handler(
        tmp_path, FakeRunner({"ps -a --format json": _ps(health="unhealthy")})
    )
    sick.handle(_req(EnumStackStepKind.RENDER))
    unhealthy = sick.handle(_req(EnumStackStepKind.READ_SERVICES))
    assert unhealthy.outcome == EnumStackStepOutcome.FAILED
    assert "demo-lane-db" in unhealthy.reading
    down_503 = _handler(tmp_path, FakeRunner(), status=503)
    down_503.handle(_req(EnumStackStepKind.RENDER))
    assert (
        down_503.handle(_req(EnumStackStepKind.READ_HEALTH, "demo-lane-db")).outcome
        == EnumStackStepOutcome.FAILED
    )
    empty_consume = _handler(tmp_path, FakeRunner())
    empty_consume.handle(_req(EnumStackStepKind.RENDER))
    event = empty_consume.handle(
        _req(EnumStackStepKind.EVENT_ROUNDTRIP, "demo-lane-db")
    )
    assert event.outcome == EnumStackStepOutcome.FAILED
    assert "consumed 0 of 1" in event.reading
    leftover = _handler(
        tmp_path,
        FakeRunner(
            {"ps -a -q": ModelCommandOutput(returncode=0, stdout="abc123\n", stderr="")}
        ),
    )
    leftover.handle(_req(EnumStackStepKind.RENDER))
    gone = leftover.handle(_req(EnumStackStepKind.READ_GONE))
    assert gone.outcome == EnumStackStepOutcome.FAILED
    assert gone.items == ("abc123",)


@pytest.mark.parametrize("kind", [EnumStackStepKind.READ_HEALTH])
def test_error_chain_health_read_for_an_undeclared_container_is_refused(
    tmp_path: Path, kind: EnumStackStepKind
) -> None:
    handler = _handler(tmp_path, FakeRunner())
    handler.handle(_req(EnumStackStepKind.RENDER))
    result = handler.handle(_req(kind, "no-such-container"))
    assert (result.outcome, result.reason) == (
        EnumStackStepOutcome.REFUSED,
        EnumStackRefusalReason.UNKNOWN_STEP,
    )
