# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Consumer-flow snapshot publishes follow the contract's event-time budget."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import yaml

from omnimarket.nodes.node_projection_consumer_flow.handlers.handler_consumer_flow_runner import (
    ConsumerFlowProjectionWriter,
    MessageMeta,
)
from omnimarket.nodes.node_projection_consumer_flow.handlers.handler_projection_consumer_flow import (
    snapshot_publish_due,
)
from omnimarket.nodes.node_projection_consumer_flow.models import (
    ModelSnapshotPublishPolicy,
)
from omnimarket.projection.models import ProjectionTableConfig

pytestmark = pytest.mark.unit

_CONTRACT_PATH = (
    Path(__file__).resolve().parents[1]
    / "src/omnimarket/nodes/node_projection_consumer_flow/contract.yaml"
)
_T0 = datetime(2026, 10, 2, 12, tzinfo=UTC)
_VERDICT = ("STALLED", "UNOBSERVED")


@pytest.fixture
def contract() -> dict[str, Any]:
    return yaml.safe_load(_CONTRACT_PATH.read_text())


@pytest.fixture
def policy(contract: dict[str, Any]) -> ModelSnapshotPublishPolicy:
    return ModelSnapshotPublishPolicy.model_validate(
        contract["snapshot_publish_policy"]
    )


def test_contract_publish_policy_loads(policy: ModelSnapshotPublishPolicy) -> None:
    assert policy.verdict_columns == ("flow_state", "upstream_evidence")
    assert policy.refresh_interval_seconds == 600


def test_contract_declares_compaction_and_size_cap(contract: dict[str, Any]) -> None:
    config = contract["projection_api"]["topic_config"]["kafka_config"]
    assert config == {
        "cleanup.policy": "compact,delete",
        "retention.ms": "604800000",
        "retention.bytes": "67108864",
        "segment.bytes": "16777216",
    }


def test_first_sighting_is_due(policy: ModelSnapshotPublishPolicy) -> None:
    assert snapshot_publish_due(
        policy=policy, last_published=None, verdict=_VERDICT, window_end=_T0
    )


@pytest.mark.parametrize(
    ("seconds_later", "verdict", "due"),
    [
        (30, _VERDICT, False),
        (30, ("FLOWING", "UNOBSERVED"), True),
        (30, ("STALLED", "PRODUCED"), True),
        (599, _VERDICT, False),
        (600, _VERDICT, True),
        (-30, _VERDICT, False),
    ],
)
def test_snapshot_publish_due_uses_event_time_and_verdict(
    policy: ModelSnapshotPublishPolicy,
    seconds_later: int,
    verdict: tuple[str, ...],
    due: bool,
) -> None:
    assert (
        snapshot_publish_due(
            policy=policy,
            last_published=(_VERDICT, _T0),
            verdict=verdict,
            window_end=_T0 + timedelta(seconds=seconds_later),
        )
        is due
    )


@pytest.mark.parametrize("iso_window_end", [False, True])
def test_writer_publishes_only_first_and_changed_verdict(
    monkeypatch: pytest.MonkeyPatch, iso_window_end: bool
) -> None:
    monkeypatch.setenv("OMNIDASH_ANALYTICS_DB_URL", "postgresql://fixture/db")
    writer = ConsumerFlowProjectionWriter()
    published: list[dict[str, Any]] = []

    async def record_publish(exposure: ProjectionTableConfig, **kwargs: Any) -> bool:
        assert exposure is writer._snapshot_exposure
        published.append(kwargs)
        return True

    monkeypatch.setattr(writer, "publish_snapshot_delta", record_publish)
    meta = MessageMeta(
        partition=0,
        offset=1,
        fallback_id="heartbeat-1",
        topic="onex.evt.platform.node-heartbeat.v1",
    )
    rows = []
    for seconds_later, state in [(0, "STALLED"), (30, "STALLED"), (60, "FLOWING")]:
        window_end = _T0 + timedelta(seconds=seconds_later)
        row = {
            "consumer_group": "fixture-consumer",
            "topic": "onex.evt.fixture.input.v1",
            "window_end": window_end.isoformat() if iso_window_end else window_end,
            "flow_state": state,
            "upstream_evidence": "UNOBSERVED",
        }
        rows.append(row)
        # Each dispatch uses its own loop; the publish history must outlive it.
        assert (
            asyncio.run(
                writer._publish_snapshot_if_available(
                    row, meta, {"correlation_id": "heartbeat-1"}
                )
            )
            is None
        )

    assert len(published) == 2
    assert [call["row"] for call in published] == [rows[0], rows[2]]
    assert all(call["op"] == "upsert" for call in published)
    assert all(call["source_event_id"] == "heartbeat-1" for call in published)
    assert all(call["source_topic"] == meta.topic for call in published)
    assert all(call["source_partition"] == meta.partition for call in published)
    assert all(call["source_offset"] == meta.offset for call in published)
    assert writer._last_published == {
        ("fixture-consumer", "onex.evt.fixture.input.v1"): (
            ("FLOWING", "UNOBSERVED"),
            _T0 + timedelta(seconds=60),
        )
    }


def test_writer_requires_contract_publish_policy(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, contract: dict[str, Any]
) -> None:
    monkeypatch.setenv("OMNIDASH_ANALYTICS_DB_URL", "postgresql://fixture/db")
    del contract["snapshot_publish_policy"]
    path = tmp_path / "contract.yaml"
    path.write_text(yaml.safe_dump(contract))

    with pytest.raises(KeyError, match="snapshot_publish_policy"):
        ConsumerFlowProjectionWriter(contract_path=path)


def test_writer_rejects_verdict_column_outside_exposure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, contract: dict[str, Any]
) -> None:
    monkeypatch.setenv("OMNIDASH_ANALYTICS_DB_URL", "postgresql://fixture/db")
    contract["snapshot_publish_policy"]["verdict_columns"].append("missing_verdict")
    path = tmp_path / "contract.yaml"
    path.write_text(yaml.safe_dump(contract))

    with pytest.raises(ValueError, match="missing_verdict"):
        ConsumerFlowProjectionWriter(contract_path=path)
