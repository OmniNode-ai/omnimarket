# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The automation-liveness exposures answer a typed read over the local store (OMN-20802).

The writer stores into a SQLite file and ``node_projection_read_effect`` reads
that file through the contract's own exposures, the path ``onex dashboard``
takes. Each test names the failure it exists to catch:

* the local store lacks one of the three relations, so the read answers
  ``projection_table_missing`` instead of rows;
* an exposure declares a column the stored relation does not have, which SQLite
  would read as a string literal;
* a declared process that never emitted is absent from the typed read;
* the read serves a column the contract does not declare.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from omnimarket.models.liveness.model_automation_liveness import (
    EnumAutomationLivenessEvent as Kind,
)
from omnimarket.nodes.node_projection_automation_liveness.handlers.handler_automation_liveness_writer import (
    AutomationLivenessProjectionWriter,
)
from omnimarket.nodes.node_projection_automation_liveness.ports.liveness_store import (
    SqliteLivenessStore,
)
from omnimarket.nodes.node_projection_read_effect.handlers.handler_projection_read import (
    HandlerProjectionRead,
)
from omnimarket.nodes.node_projection_read_effect.models import (
    ModelProjectionReadRequest,
)
from omnimarket.projection.discovery import load_projection_exposures_from_contract
from omnimarket.projection.models import ProjectionTableConfig
from tests.helpers.automation_liveness_stream import (
    ACTIVE_HOST,
    ACTIVE_PROCESS,
    SILENT_HOST,
    SILENT_PROCESS,
    STREAM_ORDER,
    fixture_payload,
    fixture_topic,
)

pytestmark = pytest.mark.unit

_CONTRACT = (
    Path(__file__).resolve().parents[1]
    / "src/omnimarket/nodes/node_projection_automation_liveness/contract.yaml"
)
_OVERLAY_ENV = "OMNIMARKET_PROJECTION_RUNTIME_BINDING_OVERLAY"
_STATE_TOPIC = "onex.snapshot.projection.automation-liveness.v1"
_RUNS_TOPIC = "onex.snapshot.projection.automation-liveness-runs.v1"
_ALARMS_TOPIC = "onex.snapshot.projection.automation-liveness-alarms.v1"


def _topic_map() -> dict[str, ProjectionTableConfig]:
    """The exposures as the contract declares them, by the loader the topic map uses."""
    contract = yaml.safe_load(_CONTRACT.read_text(encoding="utf-8"))
    return {
        e.topic: e
        for e in load_projection_exposures_from_contract(
            contract, "projection_automation_liveness", _CONTRACT
        )
    }


@pytest.fixture
def served(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> HandlerProjectionRead:
    """A store the writer filled with the fixture stream, bound as the local profile."""
    db_path = tmp_path / "liveness.sqlite"
    writer = AutomationLivenessProjectionWriter(store=SqliteLivenessStore(db_path))
    for offset, kind in enumerate(STREAM_ORDER):
        writer.handle(
            {
                **fixture_payload(kind),
                "_topic": fixture_topic(kind),
                "_partition": 0,
                "_offset": offset,
            }
        )
    overlay = tmp_path / "projection_binding.yaml"
    overlay.write_text(
        f"kafka_bootstrap_servers: inmemory\ndatabase_url: 'sqlite:///{db_path}'\n",
        encoding="utf-8",
    )
    monkeypatch.setenv(_OVERLAY_ENV, str(overlay))
    return HandlerProjectionRead(topic_map=_topic_map())


def test_automation_liveness_exposure_read_declares_three_bus_backed_exposures() -> (
    None
):
    exposures = _topic_map()
    assert set(exposures) == {_STATE_TOPIC, _RUNS_TOPIC, _ALARMS_TOPIC}
    assert all(e.bus_backed and e.key_columns for e in exposures.values())


async def test_automation_liveness_exposure_read_serves_every_declared_process(
    served: HandlerProjectionRead,
) -> None:
    result = await served.handle(ModelProjectionReadRequest(topic=_STATE_TOPIC))
    await served.close()
    assert result.ok is True, result
    by_process = {row["process_id"]: row for row in result.rows}
    assert set(by_process) == {
        ACTIVE_PROCESS,
        SILENT_PROCESS,
        "host-c/watchdog-primary",
    }
    silent = by_process[SILENT_PROCESS]
    assert silent["host"] == SILENT_HOST
    assert silent["declared_at"] is not None
    assert silent["last_run_at"] is None
    assert silent["verdict"] is None
    active = by_process[ACTIVE_PROCESS]
    assert active["host"] == ACTIVE_HOST
    assert active["verdict"] == "missed"
    assert active["last_outcome"] == "ok"
    assert active["last_demand_count"] == 4
    assert active["open_episode_id"] is None
    declared = _topic_map()[_STATE_TOPIC].columns
    assert set(active) == set(declared), "only declared columns are served"


async def test_automation_liveness_exposure_read_serves_runs_and_alarm_episodes(
    served: HandlerProjectionRead,
) -> None:
    runs = await served.handle(ModelProjectionReadRequest(topic=_RUNS_TOPIC))
    alarms = await served.handle(ModelProjectionReadRequest(topic=_ALARMS_TOPIC))
    await served.close()
    assert runs.ok is True, runs
    assert [r["run_id"] for r in runs.rows] == [
        "example-interval-job:2026-10-09T02:10:00Z"
    ]
    assert alarms.ok is True, alarms
    assert len(alarms.rows) == 1
    episode = alarms.rows[0]
    assert episode["process_id"] == ACTIVE_PROCESS
    assert episode["delivery_ref"] == "1760002290.000100"
    assert episode["recorded_by"] == "host-d/watchdog-deadman"
    assert episode["cleared_at"] is not None


def test_automation_liveness_exposure_read_stream_covers_every_event_kind() -> None:
    assert set(STREAM_ORDER) == set(Kind)
