# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A host whose mounted data drives hold under 10 percent of its lane path bytes alarms with an owner (OMN-25931)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from omnimarket.handlers.ledger_row_grammar import parse_row
from omnimarket.nodes.node_failure_alarm_router_effect.handler import (
    HandlerFailureAlarmRouter,
)
from omnimarket.nodes.node_failure_alarm_router_effect.models import (
    ModelFailureAlarmRequest,
)
from omnimarket.nodes.node_host_lane_path_share_compute.models.model_lane_path_share import (
    ModelHostLanePaths,
    ModelLanePathReading,
    ModelLanePathShareRequest,
)
from omnimarket.nodes.node_host_lane_path_share_compute.node import (
    NodeHostLanePathShareCompute,
)
from tests.nodes.node_failure_alarm_router_effect.helpers import (
    ORCHESTRATOR,
    FakePorts,
    orchestrator_claim,
)

pytestmark = pytest.mark.unit

NOW = datetime(2026, 11, 23, 18, 0, tzinfo=UTC)
NVME = "/mnt/h053-nvme"
OWNER = "orchestrator-31d9-swarm"


def _host(percent_on_drive: int, *, total: int = 100) -> ModelHostLanePaths:
    """A host with ``total`` bytes of lane paths, ``percent_on_drive`` percent of them on the NVMe mount."""
    on_drive = total * percent_on_drive // 100
    return ModelHostLanePaths(
        host_name="h053",
        owner=OWNER,
        data_drive_mounts=(NVME, "/mnt/h053-nvme2"),
        paths=(
            ModelLanePathReading(
                kind="run-dir",
                path="/data/lab-run/runs",
                mount_point=NVME,
                bytes_used=on_drive,
            ),
            ModelLanePathReading(
                kind="worktrees",
                path="/srv/lab/worktrees",
                mount_point="/",
                bytes_used=total - on_drive,
            ),
        ),
    )


def _run(host: ModelHostLanePaths):
    return NodeHostLanePathShareCompute().handle(
        ModelLanePathShareRequest(now=NOW, hosts=(host,))
    )


def test_nine_percent_on_a_data_drive_raises_the_alarm_row_with_owner() -> None:
    result = _run(_host(9))
    assert result.status == "success"
    assert result.hosts[0].state == "alarm"
    assert len(result.rows) == 1
    row = parse_row(result.rows[0])
    assert row is not None
    assert row.value("alarm") == "lane-path-share"
    assert row.value("owner") == OWNER
    assert row.value("host") == "h053"
    assert row.value("share_pct") == "9"


@pytest.mark.parametrize("percent", [11, 10, 40, 100])
def test_eleven_percent_and_over_does_not_alarm(percent: int) -> None:
    result = _run(_host(percent))
    assert result.hosts[0].state == "ok"
    row = parse_row(result.rows[0])
    assert row is not None
    assert row.value("alarm") == "lane-path-share-ok"
    assert all(" alarm=lane-path-share " not in r for r in result.rows)


def test_zero_percent_alarms_and_zero_bytes_is_refused_not_ok() -> None:
    assert _run(_host(0)).hosts[0].state == "alarm"
    empty = _run(_host(50, total=0))
    assert empty.status == "refused"
    assert empty.exit_code == 65
    assert empty.rows == ()


def test_share_is_by_bytes_so_one_small_path_on_the_drive_does_not_hide_a_full_root() -> (
    None
):
    host = ModelHostLanePaths(
        host_name="h053",
        owner=OWNER,
        data_drive_mounts=(NVME,),
        paths=(
            ModelLanePathReading(
                kind="tmp",
                path="/data/lab-run/runs/.tmp",
                mount_point=NVME,
                bytes_used=1,
            ),
            ModelLanePathReading(
                kind="codex-home",
                path="/srv/lab/agent-home",
                mount_point="/",
                bytes_used=50,
            ),
            ModelLanePathReading(
                kind="worktrees",
                path="/srv/lab/worktrees",
                mount_point="/",
                bytes_used=49,
            ),
        ),
    )
    assert _run(host).hosts[0].state == "alarm"


def test_alarm_row_reaches_the_live_orchestrator_and_push_through_the_failure_alarm_router() -> (
    None
):
    row = _run(_host(9)).rows[0]
    ports = FakePorts([orchestrator_claim(), row])
    result = HandlerFailureAlarmRouter(ports=ports).handle(
        ModelFailureAlarmRequest(now=datetime(2026, 11, 23, 18, 1, tzinfo=UTC))
    )
    assert [a.cause for a in result.alarms] == ["lane-path-share:h053"]
    written = [
        r for _, rows in ports.appends for r in rows if "alarm=failure-cause" in r
    ]
    assert len(written) == 1
    parsed = parse_row(written[0])
    assert parsed is not None
    assert parsed.value("owner") == OWNER
    assert parsed.value("to") == ORCHESTRATOR
    assert len(ports.pushed) == 1
    assert f"owner {OWNER}" in ports.pushed[0]


def test_an_ok_row_after_the_alarm_resolves_it_and_the_alarm_is_not_repeated() -> None:
    alarm = _run(_host(9)).rows[0]
    ports = FakePorts([orchestrator_claim(), alarm])
    router = HandlerFailureAlarmRouter(ports=ports)
    router.handle(
        ModelFailureAlarmRequest(now=datetime(2026, 11, 23, 18, 1, tzinfo=UTC))
    )
    again = router.handle(
        ModelFailureAlarmRequest(now=datetime(2026, 11, 23, 18, 2, tzinfo=UTC))
    )
    assert again.alarms == ()
    ok = (
        NodeHostLanePathShareCompute()
        .handle(
            ModelLanePathShareRequest(
                now=datetime(2026, 11, 23, 19, 0, tzinfo=UTC), hosts=(_host(60),)
            )
        )
        .rows[0]
    )
    ports.rows.append(ok)
    done = router.handle(
        ModelFailureAlarmRequest(now=datetime(2026, 11, 23, 19, 1, tzinfo=UTC))
    )
    assert [r.cause for r in done.resolved] == ["lane-path-share:h053"]
