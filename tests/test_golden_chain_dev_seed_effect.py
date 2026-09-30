# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden chain for node_dev_seed_effect (OMN-19970).

The broker path end to end, without a broker: ``onex seed --bus kafka`` hands
the node's wire messages to the lane bus on the delegate-skill completed topic;
each value is unwrapped the way the projection runner unwraps it; the envelope
tag is read with ``envelope_data_source``; and the real delegation projection
writes the row. It fails if the seed publishes anywhere but
``onex.evt.omnimarket.delegate-skill-completed.v1``, or if the fixture tag is lost
between the envelope and the row.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from omnimarket.cli import cli_seed
from omnimarket.models.delegation.wire.model_delegate_skill_terminal_projection import (
    ModelDelegateSkillTerminalProjection,
)
from omnimarket.nodes.node_dev_seed_effect.handlers.handler_dev_seed import (
    HandlerDevSeed,
)
from omnimarket.nodes.node_projection_delegation.handlers.handler_projection_delegation import (
    HandlerProjectionDelegation,
)
from omnimarket.projection.envelope import (
    envelope_data_source,
    strip_runner_injected_keys,
    unwrap_envelope,
)
from omnimarket.projection.protocol_database import InmemoryDatabaseAdapter

pytestmark = pytest.mark.unit

_TOPIC = "onex.evt.omnimarket.delegate-skill-completed.v1"


class _CapturingBus:
    def __init__(self) -> None:
        self.published: list[tuple[str, bytes | None, bytes]] = []

    async def publish(
        self, topic: str, key: bytes | None, value: bytes, headers: Any = None
    ) -> object:
        self.published.append((topic, key, value))
        return None


class _NullPublisher:
    def publish(self, *args: object, **kwargs: object) -> bool:
        return True


def test_golden_chain_seed_publishes_then_projects_labelled_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = _CapturingBus()

    @asynccontextmanager
    async def _fake_open(
        *,
        bus: str,
        lane: str | None,
        kafka_bootstrap: str | None,
        omni_home: Path | None,
    ) -> AsyncIterator[_CapturingBus]:
        assert bus == "kafka"
        assert kafka_bootstrap == "broker.invalid:9092"
        yield captured

    monkeypatch.setattr(cli_seed, "open_lab_run_bus", _fake_open)

    result = CliRunner().invoke(
        cli_seed.seed_command,
        [
            "--bus",
            "kafka",
            "--kafka-bootstrap",
            "broker.invalid:9092",
            "--tenant",
            "omninode",
        ],
    )
    assert result.exit_code == 0, result.output

    expected = len(HandlerDevSeed().load_fixture_set().runs)
    assert len(captured.published) == expected
    assert {topic for topic, _key, _value in captured.published} == {_TOPIC}

    db = InmemoryDatabaseAdapter()
    projection = HandlerProjectionDelegation(publisher=_NullPublisher())
    for _topic, key, value in captured.published:
        data = unwrap_envelope(value)
        assert data is not None
        assert key == str(data["correlation_id"]).encode("utf-8")
        terminal = ModelDelegateSkillTerminalProjection.from_payload(
            strip_runner_injected_keys(data)
        )
        projection.project_delegate_skill_terminal(
            terminal, db, data_source=envelope_data_source(data)
        )

    rows = db.query("delegation_events")
    assert len(rows) == expected
    assert {row["data_source"] for row in rows} == {"fixture"}


def test_golden_chain_inmemory_bus_refuses_a_broker_argument() -> None:
    result = CliRunner().invoke(
        cli_seed.seed_command, ["--bus", "inmemory", "--lane", "dev"]
    )
    assert result.exit_code != 0
    assert "--bus kafka" in result.output


def test_seed_names_its_workspace_root_option_omnibase_path_not_omni_home() -> None:
    """The customer surface carries no internal `omni-home` parameter (the C17 parameter audit)."""
    names = {opt for param in cli_seed.seed_command.params for opt in param.opts}
    assert "--omnibase-path" in names
    assert "--omni-home" not in names
    result = CliRunner().invoke(cli_seed.seed_command, ["--omni-home", "x"])
    assert result.exit_code != 0
    assert "No such option" in result.output
