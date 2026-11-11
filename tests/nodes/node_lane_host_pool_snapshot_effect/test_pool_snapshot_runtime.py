# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The lane host pool snapshot step on its real backend (OMN-23664).

One pass of the runtime reads the four sources, projects the per-host rows, and, when the
request writes, stores the snapshot and record and publishes the record on the record topic.
Each pass that writes a snapshot emits one chain (start, then success or failure) to the floor's
emission journal under the request's correlation id.

Golden: a start then success under one correlation id, read back from the JSONL.
Error: a failing source ends in a failure chain with the reason as the failure code.
Exit codes: 0 for written or observed, 1 for the typed failed event.
--probe prints a probe_ok JSON line and exits 0. --observe writes no file.
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from omnimarket.nodes.node_lane_host_pool_snapshot_effect import (
    runtime_pool_snapshot as runtime,
)
from omnimarket.nodes.node_lane_host_pool_snapshot_effect.handlers.pool_snapshot_ports import (
    InMemoryHostAccountsSource,
    InMemoryHostAdvertisementSource,
    InMemoryHostTableSource,
    InMemoryPoolSnapshotPublisher,
    InMemoryPoolSnapshotStore,
    InMemoryRunningLanesSource,
)
from omnimarket.nodes.node_lane_host_pool_snapshot_effect.handlers.pool_snapshot_wiring import (
    PoolSnapshotPorts,
)
from omnimarket.nodes.node_lane_host_pool_snapshot_effect.handlers.snapshot_errors import (
    PoolSnapshotSourceError,
)
from omnimarket.nodes.node_lane_host_pool_snapshot_effect.models.model_pool_snapshot_overlay import (
    ModelPoolSnapshotOverlay,
)
from omnimarket.nodes.node_lane_host_pool_snapshot_effect.runtime_pool_snapshot import (
    main,
    run_once,
)
from tests.nodes.node_lane_host_pool_snapshot_effect.pool_inputs import (
    accounts,
    advertisement,
    clock,
    host_row,
    overlay,
)

pytestmark = pytest.mark.unit


def _ports(
    *,
    ads=(),
    rows=(),
    lanes=None,
    accs=(),
    store=None,
    publisher=None,
) -> PoolSnapshotPorts:
    return PoolSnapshotPorts(
        advertisements=InMemoryHostAdvertisementSource(tuple(ads)),
        host_table=InMemoryHostTableSource(tuple(rows)),
        lanes=InMemoryRunningLanesSource(lanes or {}),
        accounts=InMemoryHostAccountsSource(tuple(accs)),
        store=store if store is not None else InMemoryPoolSnapshotStore(),
        publisher=publisher
        if publisher is not None
        else InMemoryPoolSnapshotPublisher(),
    )


def _ov(tmp: Path, *, emission_journal: str | None = None) -> ModelPoolSnapshotOverlay:
    return overlay(
        snapshot_path=str(tmp / "pool.json"),
        emission_journal=emission_journal,
    )


def _emissions(tmp: Path) -> list[dict[str, object]]:
    return [
        json.loads(line) for line in (tmp / "emissions.jsonl").read_text().splitlines()
    ]


def test_golden_start_then_success_under_one_correlation_id(tmp_path: Path) -> None:
    journal = tmp_path / "emissions.jsonl"
    ov = _ov(tmp_path, emission_journal=str(journal))
    ports = _ports(
        ads=(advertisement("h1"),),
        rows=(host_row("h1"),),
        lanes={"h1": 1},
        accs=(accounts("h1"),),
    )
    code, line = run_once(
        ov,
        ports=ports,
        now=clock(),
        write=True,
    )
    assert code == 0
    assert line["status"] == "written"
    assert line["hosts"] == 1
    # h1: 8 cores // 2 cores per lane = 4 slots, 1 running lane, 3 free.
    assert line["free_slots_total"] == 3
    entries = _emissions(tmp_path)
    assert len(entries) == 2
    assert entries[0]["phase"] == "start"
    assert entries[0]["correlation_id"] == str(line["correlation_id"]).replace("-", "")
    assert entries[1]["phase"] == "success"
    assert entries[1]["correlation_id"] == str(line["correlation_id"]).replace("-", "")
    assert entries[1]["node"] == "node_lane_host_pool_snapshot_effect"
    assert entries[1]["piece"] == 2


def test_error_failing_source_ends_in_failure_chain_with_reason_as_failure_code(
    tmp_path: Path,
) -> None:
    journal = tmp_path / "emissions.jsonl"
    ov = _ov(tmp_path, emission_journal=str(journal))

    class _FailingAds:
        name = "failing-ads"

        def read_advertisements(self):
            raise PoolSnapshotSourceError("advertisements unreadable")

    ports = replace(
        _ports(rows=(host_row("h1"),), lanes={"h1": 0}, accs=(accounts("h1"),)),
        advertisements=_FailingAds(),
    )
    code, line = run_once(
        ov,
        ports=ports,
        now=clock(),
        write=True,
    )
    assert code == 1
    assert line["status"] == "failed"
    assert line["failure_reason"] == "advertisements_unread"
    assert "advertisements unreadable" in str(line["detail"])
    entries = _emissions(tmp_path)
    assert len(entries) == 2
    assert entries[0]["phase"] == "start"
    assert entries[1]["phase"] == "failure"
    assert entries[1]["failure_code"] == "advertisements_unread"
    assert entries[1]["correlation_id"] == str(line["correlation_id"]).replace("-", "")


def test_exit_code_zero_for_observed(tmp_path: Path) -> None:
    ov = _ov(tmp_path)
    ports = _ports(
        ads=(advertisement("h1"),),
        rows=(host_row("h1"),),
        lanes={"h1": 0},
        accs=(accounts("h1"),),
    )
    code, line = run_once(
        ov,
        ports=ports,
        now=clock(),
        write=False,
    )
    assert code == 0
    assert line["status"] == "observed"
    assert "read_at" in line
    assert "rows" in line


def test_main_probe_prints_probe_ok_and_exits_zero() -> None:
    code = main(["--probe"])
    assert code == 0


def test_main_observe_prints_the_rows_and_writes_no_file(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ov = _ov(tmp_path, emission_journal=str(tmp_path / "emissions.jsonl"))
    overlay_file = tmp_path / "overlay.yaml"
    overlay_file.write_text(json.dumps(ov.model_dump(mode="json")), encoding="utf-8")
    ports = _ports(
        ads=(advertisement("h1"),),
        rows=(host_row("h1"),),
        accs=(accounts("h1"),),
    )
    monkeypatch.setenv("ONEX_POOL_SNAPSHOT_OVERLAY_FILE", str(overlay_file))
    monkeypatch.setattr(
        runtime, "build_pool_snapshot_ports", lambda *_args, **_kwargs: ports
    )
    assert main(["--observe"]) == 0
    parsed = json.loads(capsys.readouterr().out.strip())
    assert parsed["status"] == "observed"
    assert parsed["rows"][0]["host"] == "h1"
    assert not (tmp_path / "pool.json").exists()
    assert not (tmp_path / "emissions.jsonl").exists()
    assert ports.store.read_record() is None  # type: ignore[attr-defined]


def test_main_without_a_declared_overlay_is_refused_with_exit_three(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ONEX_POOL_SNAPSHOT_OVERLAY_FILE", raising=False)
    monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", str(tmp_path))
    assert main([]) == 3
    assert json.loads(capsys.readouterr().out.strip())["status"] == "overlay_refused"
