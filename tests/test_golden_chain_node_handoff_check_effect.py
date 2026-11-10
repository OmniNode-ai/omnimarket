# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden chain for node_handoff_check_effect (OMN-21910).

One command on the in-memory bus in, one completed event out, through the handler the node's
contract names, with a recorded ledger, recorded probes and no live service. The chain covers
the refusal (exit 1, nothing written) and the accepted handoff (exit 0, one file written).
Topics are read from the node's contract.yaml.
"""

from __future__ import annotations

import importlib
import json
from pathlib import Path
from typing import Any

import pytest
import yaml
from omnibase_core.event_bus.event_bus_inmemory import EventBusInmemory

from omnimarket.nodes.node_handoff_check_effect.handlers.handler_handoff_check import (
    HandlerHandoffCheck,
)
from omnimarket.nodes.node_handoff_check_effect.models import (
    EnumHandoffCommand,
    ModelHandoffCheckRequest,
    ModelHandoffCheckResult,
)

_CONTRACT = (
    Path(__file__).resolve().parents[1]
    / "src/omnimarket/nodes/node_handoff_check_effect/contract.yaml"
)
_CONTRACT_DATA = yaml.safe_load(_CONTRACT.read_text(encoding="utf-8"))
COMMAND_TOPIC = _CONTRACT_DATA["event_bus"]["subscribe_topics"][0]
COMPLETED_TOPIC = _CONTRACT_DATA["event_bus"]["publish_topics"][0]
_NOW = "2026-11-01T23:00:00Z"
_RULING = (
    '2026-11-01T20:00:00Z | RULING | lane=orch | ticket=TKT-1 | question=planted | "go"'
)
_CLAIM = "2026-11-01T20:30:00Z | CLAIM | lane=other | ticket=TKT-9 | actor=x | model=x"
_PROBE = {
    "name": "dev-bus",
    "healthy": False,
    "detail": "ConnectionRefusedError",
    "read_at": _NOW,
    "probe": "recorded dev-bus",
}


def _request(
    tmp_path: Path, *rows: str, out_dir: Path | None
) -> ModelHandoffCheckRequest:
    ledger = tmp_path / "ledger.md"
    ledger.write_text("\n".join(rows) + "\n", encoding="utf-8")
    probes = tmp_path / "probes.json"
    probes.write_text(json.dumps([_PROBE]), encoding="utf-8")
    return ModelHandoffCheckRequest(
        command=EnumHandoffCommand.ASSEMBLE,
        ledger=str(ledger),
        archive_dir=str(tmp_path / "none"),
        now=_NOW,
        probes=str(probes),
        out_dir=str(out_dir) if out_dir else None,
    )


def test_the_contract_names_the_handler_and_the_topics() -> None:
    module = importlib.import_module(_CONTRACT_DATA["handler"]["module"])
    assert getattr(module, _CONTRACT_DATA["handler"]["class"]) is HandlerHandoffCheck
    assert COMMAND_TOPIC == "onex.cmd.omnimarket.handoff-check-start.v1"
    assert COMPLETED_TOPIC == "onex.evt.omnimarket.handoff-check-completed.v1"
    assert _CONTRACT_DATA["terminal_event"] == COMPLETED_TOPIC


async def _round_trip(request: ModelHandoffCheckRequest) -> ModelHandoffCheckResult:
    event_bus = EventBusInmemory()
    handler = HandlerHandoffCheck()
    completed: list[dict[str, Any]] = []

    async def on_completed(message: Any) -> None:
        completed.append(json.loads(message.value))

    async def on_command(message: Any) -> None:
        result = handler.handle(
            ModelHandoffCheckRequest.model_validate(json.loads(message.value))
        )
        await event_bus.publish(
            COMPLETED_TOPIC, key=None, value=result.model_dump_json().encode()
        )

    await event_bus.start()
    await event_bus.subscribe(COMPLETED_TOPIC, on_message=on_completed, group_id="done")
    await event_bus.subscribe(COMMAND_TOPIC, on_message=on_command, group_id="check")
    await event_bus.publish(
        COMMAND_TOPIC, key=None, value=request.model_dump_json().encode()
    )
    assert len(completed) == 1
    await event_bus.close()
    return ModelHandoffCheckResult.model_validate(completed[0])


async def test_a_planted_ruling_command_in_a_refusal_event_out(tmp_path: Path) -> None:
    out_dir = tmp_path / "out"
    result = await _round_trip(_request(tmp_path, _RULING, _CLAIM, out_dir=out_dir))
    assert result.exit_code == 1
    assert len(result.refused) == 1
    assert not out_dir.exists()
    # Every output the contract declares is on the completed event.
    payload = result.model_dump(mode="json")
    for key in ("exit_code", "output", "refused", "stale_lines", "written_path"):
        assert key in payload
    assert set(_CONTRACT_DATA["outputs"]) == {
        "exit_code",
        "output",
        "refused",
        "stale_lines",
        "written_path",
    }


async def test_an_accounted_handoff_command_in_a_written_event_out(
    tmp_path: Path,
) -> None:
    out_dir = tmp_path / "out"
    result = await _round_trip(_request(tmp_path, _CLAIM, out_dir=out_dir))
    # An unhealthy service is recorded and never a refusal.
    assert result.exit_code == 0
    assert result.written_path is not None
    written = Path(result.written_path).read_text(encoding="utf-8")
    assert "dev-bus: UNHEALTHY (ConnectionRefusedError)" in written
    assert [p.name for p in out_dir.iterdir()] == [Path(result.written_path).name]


@pytest.mark.parametrize("command", ["resume", "record-slice"])
async def test_a_command_missing_its_inputs_is_bad_input_not_a_crash(
    tmp_path: Path, command: str
) -> None:
    request = _request(tmp_path, _CLAIM, out_dir=None).model_copy(
        update={"command": EnumHandoffCommand(command)}
    )
    result = await _round_trip(request)
    assert result.exit_code == 2
    assert result.errors


def test_handoff_check_deploy_registers_the_node_and_console_script() -> None:
    import tomllib

    pyproject = tomllib.loads(
        (Path(__file__).resolve().parents[1] / "pyproject.toml").read_text()
    )
    scripts = pyproject["project"]["scripts"]
    nodes = pyproject["project"]["entry-points"]["onex.nodes"]
    assert scripts["onex-handoff-check"] == (
        "omnimarket.nodes.node_handoff_check_effect.runtime_handoff_check_cli:main"
    )
    assert nodes["node_handoff_check_effect"] == (
        "omnimarket.nodes.node_handoff_check_effect"
    )
    module = importlib.import_module(
        "omnimarket.nodes.node_handoff_check_effect.runtime_handoff_check_cli"
    )
    assert callable(module.main)
