# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-22988: the hourly unit's Linear snapshot equals the node's on a recorded hour.

The hourly launchd unit runs ``python -m omnimarket.nodes.node_linear_snapshot_effect --out-dir <dir>``
(the host's morning-workflows driver, workflow ``linear-snapshot``). No second snapshot
implementation is left to compare against, so the unit side of the parity is the unit's own entry
point, ``main``, writing the file; the node side is the handler called with typed requests. Both read
the same recorded hour (``fixtures/recorded_hour.json``: the GraphQL pages of one hourly read).
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from omnimarket.nodes.node_hourly_tick_effect.handlers.handler_hourly_tick import (
    HandlerHourlyTick,
)
from omnimarket.nodes.node_hourly_tick_effect.models.model_hourly_tick import (
    ModelHourlyTickRequest,
)
from omnimarket.nodes.node_linear_snapshot_effect.handlers import (
    handler_linear_snapshot as handler_module,
)
from omnimarket.nodes.node_linear_snapshot_effect.handlers.handler_linear_snapshot import (
    HandlerLinearSnapshot,
    SnapshotReadError,
)
from omnimarket.nodes.node_linear_snapshot_effect.models.model_linear_snapshot import (
    ModelLinearSnapshotRequest,
)
from omnimarket.nodes.node_linear_snapshot_effect.runtime_linear_snapshot import main

RECORDED = json.loads(
    (Path(__file__).parent / "fixtures" / "recorded_hour.json").read_text(
        encoding="utf-8"
    )
)
DATE = RECORDED["date"]
COMPARED_FIELDS = ("title", "assignee", "labels", "description")


class RecordedHour:
    """The recorded pages, answered in order; ``hasNextPage`` is true until the last."""

    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, query: str, variables: dict[str, Any]) -> dict[str, Any]:
        page = RECORDED["pages"][self.calls]
        self.calls += 1
        last = self.calls == len(RECORDED["pages"])
        return {
            "viewer": RECORDED["viewer"],
            "issues": {
                "pageInfo": {
                    "hasNextPage": not last,
                    "endCursor": None if last else f"cursor-{self.calls}",
                },
                "nodes": page,
            },
        }


def node_snapshot(out_dir: Path) -> dict[str, Any]:
    result = HandlerLinearSnapshot(RecordedHour()).handle(
        ModelLinearSnapshotRequest(out_dir=str(out_dir), date=DATE)
    )
    assert result.status == "written"
    snapshot: dict[str, Any] = json.loads(
        (out_dir / f"linear-{DATE}.json").read_text(encoding="utf-8")
    )
    return snapshot


def unit_snapshot(
    out_dir: Path, monkeypatch: pytest.MonkeyPatch, post: Any = None
) -> dict[str, Any]:
    """Run the unit's entry point with the recorded hour behind its Linear transport."""
    monkeypatch.setattr(handler_module, "http_post", post or RecordedHour())
    assert main(["--out-dir", str(out_dir), "--date", DATE]) == 0
    snapshot: dict[str, Any] = json.loads(
        (out_dir / f"linear-{DATE}.json").read_text(encoding="utf-8")
    )
    return snapshot


def assert_same_snapshot(unit: dict[str, Any], node: dict[str, Any]) -> None:
    """Field for field: operator, then each issue's title, assignee, labels and description."""
    assert unit["operator"] == node["operator"], "operator differs"
    unit_by_id = {i["id"]: i for i in unit["issues"]}
    node_by_id = {i["id"]: i for i in node["issues"]}
    assert unit_by_id.keys() == node_by_id.keys(), "the issue sets differ"
    for ident, expected in node_by_id.items():
        for field in COMPARED_FIELDS:
            assert unit_by_id[ident][field] == expected[field], (
                f"{ident} {field} differs"
            )
    assert unit == node


def test_parity_with_unit_on_recorded_hour(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    node = node_snapshot(tmp_path / "node")
    unit = unit_snapshot(tmp_path / "unit", monkeypatch)
    assert len(node["issues"]) == RECORDED["expected_issue_count"] > 2
    assert any(i["labels"] for i in node["issues"])
    assert any(i["description"] for i in node["issues"])
    assert any(i["description"] == "" for i in node["issues"])
    assert_same_snapshot(unit, node)


def test_planted_divergence_turns_the_parity_red(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    node = node_snapshot(tmp_path / "node")
    unit = unit_snapshot(tmp_path / "unit", monkeypatch)
    assert_same_snapshot(unit, node)  # green before the plant

    for field, planted in (
        ("title", "A different title"),
        ("assignee", "00000000-0000-4000-8000-0000000000ff"),
        ("labels", ["a-label-the-node-did-not-read"]),
        ("description", "A different description"),
    ):
        diverged = json.loads(json.dumps(unit))
        diverged["issues"][0][field] = planted
        with pytest.raises(AssertionError, match=f"{field} differs"):
            assert_same_snapshot(diverged, node)

    dropped = json.loads(json.dumps(unit))
    dropped["issues"].pop()
    with pytest.raises(AssertionError, match="issue sets differ"):
        assert_same_snapshot(dropped, node)


def test_planted_divergence_in_the_unit_reader_turns_the_parity_red(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The plant is in the reader the unit runs, not in a copy of its output."""
    node = node_snapshot(tmp_path / "node")
    real_issue = handler_module._issue

    def drops_the_first_label(*args: Any, **kwargs: Any) -> Any:
        issue = real_issue(*args, **kwargs)
        return issue.model_copy(update={"labels": issue.labels[1:]})

    monkeypatch.setattr(handler_module, "_issue", drops_the_first_label)
    unit = unit_snapshot(tmp_path / "unit", monkeypatch)
    with pytest.raises(AssertionError, match="labels differ"):
        assert_same_snapshot(unit, node)


def test_failed_snapshot_read_writes_no_snapshot_and_keeps_the_previous_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Guarantee 1 of 2: a failed Linear read writes no snapshot and leaves the earlier file.

    This is the snapshot node alone. It does not reach the hourly tick; the tick's own
    guarantee is the next test, which feeds the tick a failed sweep result by hand.
    """
    out_dir = tmp_path / "snapshots"
    yesterday = out_dir / f"linear-{DATE}.json"
    out_dir.mkdir()
    yesterday.write_text("{}", encoding="utf-8")

    def refused(query: str, variables: dict[str, Any]) -> dict[str, Any]:
        raise SnapshotReadError("Linear refused the read: rate limited")

    monkeypatch.setattr(handler_module, "http_post", refused)
    assert main(["--out-dir", str(out_dir), "--date", DATE]) == 1
    assert capsys.readouterr().out.startswith("linear-snapshot | failed ")
    assert yesterday.read_text(encoding="utf-8") == "{}"


def test_tick_given_a_failed_sweep_result_keeps_its_window_pointer(
    tmp_path: Path,
) -> None:
    """Guarantee 2 of 2: the hourly tick keeps its window pointer when its sweep result says failed.

    The failed and the successful sweep results are supplied by hand. No result of a
    snapshot invocation reaches the tick here, so this test does not show that a snapshot
    read failure propagates to the tick; the two guarantees are independent.
    """
    home = tmp_path / "home"
    home.mkdir()
    (home / "checkpoints.md").write_text(
        "- 2026-09-16T21:27:48Z 60m checkpoint: earlier\n"
    )
    (home / "pointer.txt").write_text("2026-09-16T21:20:00Z\n")
    (home / "ledger.md").write_text(
        "2026-09-16T21:31:00Z | CLAIM | lane=other | body\n"
        "2026-09-16T21:40:57Z | TERMINAL | lane=last | body\n"
    )

    def tick(**extra: object) -> Any:
        return asyncio.run(
            HandlerHourlyTick().handle(
                ModelHourlyTickRequest.model_validate(
                    {
                        "registry_root": str(home),
                        "checkpointFile": "checkpoints.md",
                        "sweepStateFile": "pointer.txt",
                        "ledgerFile": "ledger.md",
                        "now": "2026-09-16T21:40:57Z",
                        "fireId": "fire-1",
                        **extra,
                    }
                )
            )
        )

    prepared = tick()
    failed = tick(
        operation="record",
        correlation_id=prepared.correlation_id,
        state=prepared.state.model_dump(),
        sweep={"failed": True, "error_class": "linear_read"},
    )
    assert failed.phase == "degraded"
    assert (home / "pointer.txt").read_text() == "2026-09-16T21:20:00Z\n"

    # control: the same fire with a read that succeeded moves the pointer
    prepared_ok = tick(fireId="fire-2")
    tick(
        operation="record",
        fireId="fire-2",
        correlation_id=prepared_ok.correlation_id,
        state=prepared_ok.state.model_dump(),
        sweep={"collected": 0, "needing": 0, "accepted": 0, "held": 0},
    )
    assert (home / "pointer.txt").read_text() == "2026-09-16T21:40:57Z\n"
