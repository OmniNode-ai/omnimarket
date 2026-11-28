"""`onex stack status` and `onex stack stop` over the shipped compose file (OMN-26689)."""

from __future__ import annotations

import json

import pytest
from click.testing import CliRunner

from omnimarket.nodes.node_onex_setup_effect import cli_stack

pytestmark = pytest.mark.unit

_PS = "\n".join(
    json.dumps(row)
    for row in (
        {"Service": "postgres", "State": "running", "Health": "healthy"},
        {"Service": "redpanda", "State": "running", "Health": "starting"},
        {"Service": "worker", "State": "exited", "Health": ""},
    )
)


def test_status_reads_compose_ps_and_names_each_service_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[list[str]] = []

    async def fake(cmd: list[str]) -> tuple[int, str, str]:
        seen.append(cmd)
        return 0, "", _PS

    monkeypatch.setattr(cli_stack, "run_compose", fake)
    result = CliRunner().invoke(cli_stack.stack_group, ["status", "--json"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["stack"] == [
        {"name": "postgres", "state": "running"},
        {"name": "redpanda", "state": "starting"},
        {"name": "worker", "state": "exited"},
    ]
    assert seen[0][-4:] == ["ps", "--all", "--format", "json"]
    assert str(cli_stack.COMPOSE_FILE) in seen[0]


def test_stop_runs_compose_down_and_keeps_volumes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[list[str]] = []

    async def fake(cmd: list[str]) -> tuple[int, str, str]:
        seen.append(cmd)
        return 0, "", ""

    monkeypatch.setattr(cli_stack, "run_compose", fake)
    result = CliRunner().invoke(cli_stack.stack_group, ["stop"])
    assert result.exit_code == 0, result.output
    assert seen[0][-1] == "down"
    assert "-v" not in seen[0]
    assert "--volumes" not in seen[0]


def test_status_with_docker_unavailable_lists_no_services(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake(cmd: list[str]) -> tuple[int, str, str]:
        return 127, "docker not found", ""

    monkeypatch.setattr(cli_stack, "run_compose", fake)
    result = CliRunner().invoke(cli_stack.stack_group, ["status", "--json"])
    assert result.exit_code == 0
    assert json.loads(result.output)["stack"] == []
