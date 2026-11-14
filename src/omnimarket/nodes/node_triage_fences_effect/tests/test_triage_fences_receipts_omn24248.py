# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The runner receipts name the running lanes; the effect node passes them to the fence (OMN-24248)."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from omnimarket.nodes.node_triage_fences_effect.handlers.handler_triage_fences import (
    HandlerTriageFences,
)
from omnimarket.nodes.node_triage_fences_effect.logic.triage_fences_receipts import (
    running_lanes,
)
from omnimarket.nodes.node_triage_fences_effect.models import (
    ModelTriageFencesOptions,
    ModelTriageFencesOverlay,
    ModelTriageFencesRequest,
)
from omnimarket.nodes.node_triage_fences_effect.overlay import (
    load_triage_fences_overlay,
)

NOW = datetime(2026, 11, 13, 15, 0, 0, tzinfo=UTC)
MAX_AGE_MINUTES = 240.0
EXAMPLE = Path(__file__).parents[1] / "overlay.example.yaml"


def _receipt(root: Path, lane: str, run: str, **over: Any) -> Path:
    """A receipt as the lane runner writes it while a lane runs."""
    body: dict[str, Any] = {
        "lane": lane,
        "run_id": f"rlane-{lane}-{run}",
        "status": "running",
        "final": False,
        "started_at": "2026-11-13T14:00:00Z",
        "host": "h053",
        "pid": 4242,
        "parent": "orchestrator-31d9",
        "ticket": "OMN-90001",
        "model": "sonnet",
    }
    body.update(over)
    folder = root / lane
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"rlane-{lane}-{run}.json"
    path.write_text(json.dumps(body), encoding="utf-8")
    return path


def _running(root: Path) -> tuple[tuple[str, ...], tuple[str, ...]]:
    return running_lanes(root, NOW, MAX_AGE_MINUTES)


def test_a_running_receipt_counts(tmp_path: Path) -> None:
    _receipt(tmp_path, "lane-a", "aaa111")
    assert _running(tmp_path) == (("lane-a",), ())


def test_a_final_receipt_does_not_count_even_if_it_says_running(tmp_path: Path) -> None:
    _receipt(tmp_path, "lane-a", "aaa111", final=True)
    assert _running(tmp_path) == ((), ())


def test_an_ended_status_does_not_count(tmp_path: Path) -> None:
    _receipt(tmp_path, "lane-a", "aaa111", status="setup-failed", final=True)
    _receipt(tmp_path, "lane-b", "bbb222", status="placing")
    assert _running(tmp_path) == ((), ())


def test_a_running_receipt_older_than_the_limit_does_not_count(tmp_path: Path) -> None:
    _receipt(
        tmp_path, "lane-a", "aaa111", started_at="2026-11-13T10:59:00Z"
    )  # 241 minutes
    _receipt(
        tmp_path, "lane-b", "bbb222", started_at="2026-11-13T11:01:00Z"
    )  # 239 minutes
    assert _running(tmp_path) == (("lane-b",), ())


def test_the_newest_receipt_of_a_lane_wins(tmp_path: Path) -> None:
    _receipt(tmp_path, "lane-a", "aaa111", started_at="2026-11-13T13:00:00Z")
    _receipt(
        tmp_path, "lane-a", "bbb222", started_at="2026-11-13T14:00:00Z", final=True
    )
    _receipt(
        tmp_path, "lane-b", "ccc333", started_at="2026-11-13T13:00:00Z", final=True
    )
    _receipt(tmp_path, "lane-b", "ddd444", started_at="2026-11-13T14:00:00Z")
    assert _running(tmp_path) == (("lane-b",), ())


def test_a_job_file_is_not_a_receipt(tmp_path: Path) -> None:
    folder = tmp_path / "lane-a"
    folder.mkdir()
    (folder / "rlane-lane-a-aaa111.job.json").write_text(
        json.dumps(
            {
                "lane": "lane-a",
                "status": "running",
                "started_at": "2026-11-13T14:00:00Z",
            }
        ),
        encoding="utf-8",
    )
    assert _running(tmp_path) == ((), ())


def test_an_unreadable_receipt_adds_an_error_and_the_others_still_count(
    tmp_path: Path,
) -> None:
    _receipt(tmp_path, "lane-a", "aaa111")
    broken = tmp_path / "lane-b"
    broken.mkdir()
    (broken / "rlane-lane-b-bbb222.json").write_text("{not json", encoding="utf-8")
    lanes, errors = _running(tmp_path)
    assert lanes == ("lane-a",)
    assert len(errors) == 1
    assert "rlane-lane-b-bbb222.json" in errors[0]


def test_a_missing_receipt_folder_is_one_error_and_no_lanes(tmp_path: Path) -> None:
    lanes, errors = _running(tmp_path / "absent")
    assert lanes == ()
    assert len(errors) == 1


def test_a_start_time_without_a_zone_does_not_raise(tmp_path: Path) -> None:
    _receipt(tmp_path, "lane-a", "aaa111", started_at="2026-11-13T14:00:00")
    assert _running(tmp_path) == (("lane-a",), ())


def test_lane_name_falls_back_to_the_folder(tmp_path: Path) -> None:
    path = _receipt(tmp_path, "lane-a", "aaa111")
    body = json.loads(path.read_text(encoding="utf-8"))
    del body["lane"]
    path.write_text(json.dumps(body), encoding="utf-8")
    assert _running(tmp_path) == (("lane-a",), ())


# ---- the effect node: ledger, receipts and overlay to the report ------------------------------


def _overlay(receipts: Path) -> ModelTriageFencesOverlay:
    return ModelTriageFencesOverlay(
        stale_after_minutes=45,
        pr_window_hours=6,
        repos=("omnimarket",),
        receipt_dir=receipts,
        running_receipt_max_age_minutes=MAX_AGE_MINUTES,
        watcher_state=None,
    )


LEDGER = "\n".join(
    [
        "2026-11-13T09:00:00Z | CLAIM | lane=by-row | ticket=OMN-90010 | actor=script:t | x",
        "2026-11-13T14:40:00Z | STATUS | lane=by-row | working",
        "2026-11-13T09:05:00Z | CLAIM | lane=by-receipt | ticket=OMN-90011 | actor=script:t | x",
        "2026-11-13T09:10:00Z | CLAIM | lane=dead | ticket=OMN-90012 | actor=script:t | x",
    ]
)


def test_golden_chain_ledger_and_receipts_to_report(tmp_path: Path) -> None:
    ledger = tmp_path / "LEDGER.md"
    ledger.write_text(LEDGER + "\n", encoding="utf-8")
    receipts = tmp_path / "receipts"
    _receipt(receipts, "by-receipt", "aaa111", started_at="2026-11-13T13:00:00Z")
    handler = HandlerTriageFences(
        ModelTriageFencesOptions(ledger=ledger), _overlay(receipts)
    )
    result = handler.handle(ModelTriageFencesRequest(now=NOW))
    assert result.exit_code == 0
    assert result.report is not None
    report = result.report
    assert report.fenced_tickets == ("OMN-90010", "OMN-90011")
    assert {c.lane: c.live_basis for c in report.open_claims} == {
        "by-row": "recent-row",
        "by-receipt": "running-receipt",
    }
    assert [c.lane for c in report.stale_claims] == ["dead"]
    assert report.stale_after_hours == 0.75
    assert report.errors == ()


def test_error_chain_unreadable_ledger_is_refused(tmp_path: Path) -> None:
    missing = tmp_path / "nope.md"
    handler = HandlerTriageFences(
        ModelTriageFencesOptions(ledger=missing), _overlay(tmp_path)
    )
    result = handler.handle(ModelTriageFencesRequest(now=NOW))
    assert result.exit_code == 2
    assert result.report is None
    assert str(missing) in result.error


def test_error_chain_unreadable_receipt_is_reported_and_the_ledger_fences_stay(
    tmp_path: Path,
) -> None:
    ledger = tmp_path / "LEDGER.md"
    ledger.write_text(LEDGER + "\n", encoding="utf-8")
    receipts = tmp_path / "receipts"
    (receipts / "x").mkdir(parents=True)
    (receipts / "x" / "rlane-x-111111.json").write_text("[1, 2", encoding="utf-8")
    handler = HandlerTriageFences(
        ModelTriageFencesOptions(ledger=ledger), _overlay(receipts)
    )
    result = handler.handle(ModelTriageFencesRequest(now=NOW))
    assert result.report is not None
    assert result.report.fenced_tickets == ("OMN-90010",)
    assert len(result.report.errors) == 1


# ---- the overlay -----------------------------------------------------------------------------


def test_overlay_missing_refuses_naming_the_setting() -> None:
    with pytest.raises(
        ValueError, match=r"node_triage_fences_effect|ONEX_SKILL_OVERLAY_ROOTS"
    ):
        load_triage_fences_overlay(environ={})


def test_example_overlay_loads(tmp_path: Path) -> None:
    node = tmp_path / "node_triage_fences_effect"
    node.mkdir()
    (node / "overlay.yaml").write_text(
        EXAMPLE.read_text(encoding="utf-8"), encoding="utf-8"
    )
    overlay = load_triage_fences_overlay(
        environ={"ONEX_SKILL_OVERLAY_ROOTS": str(tmp_path)}
    )
    assert overlay.stale_after_minutes == 45
    assert overlay.receipt_dir.expanduser().is_absolute()
    assert yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))["watcher_state"] is None


def test_no_numeric_default_in_the_overlay_model() -> None:
    with pytest.raises(ValidationError) as excinfo:
        ModelTriageFencesOverlay.model_validate({})
    missing = {str(e["loc"][0]) for e in excinfo.value.errors()}
    assert missing == {
        "stale_after_minutes",
        "pr_window_hours",
        "repos",
        "receipt_dir",
        "running_receipt_max_age_minutes",
    }
