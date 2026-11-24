# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The daily census run publishes its record and the change against the previous day as one typed event.

The census tick prints and keeps the daily record (one row per repository and the runtime-code count) and published
nothing, so the shrink could be read only from a file on one host. The tick now puts one typed event on the bus, with the
change against the previous day's record, and keeps the dated records it reads that change from.
"""

from __future__ import annotations

import io
import json
from collections.abc import Mapping, Sequence
from pathlib import Path

import pytest
import yaml

from omnimarket.nodes.node_work_ledger_file_census_effect import (
    runtime_file_census_tick as tick,
)

pytestmark = pytest.mark.unit

PACKAGE = Path(tick.__file__).resolve().parent
TOPIC = "onex.evt.omnimarket.ledger-file-census-daily-record.v1"


class FakePublisher:
    def __init__(self, accept: bool = True) -> None:
        self.accept = accept
        self.sent: list[dict[str, object]] = []

    def publish(
        self,
        *,
        topic: str,
        event_type: str,
        payload: Mapping[str, object],
        event_id: str,
        correlation_id: str | None = None,
    ) -> bool:
        self.sent.append(
            {
                "topic": topic,
                "event_type": event_type,
                "payload": dict(payload),
                "event_id": event_id,
                "correlation_id": correlation_id,
            }
        )
        return self.accept


def _row(
    repository: str,
    files: int,
    runtime: int,
    ratchet: int | None = None,
    state: str = "ok",
) -> dict[str, object]:
    return {
        "repository": repository,
        "own_line": False,
        "files": files,
        "runtime_files": runtime,
        "ratchet": files if ratchet is None else ratchet,
        "state": state,
    }


def _record(
    day: str,
    rows: Sequence[dict[str, object]],
    *,
    passed: bool = True,
    failure_code: str | None = None,
) -> dict[str, object]:
    return {
        "correlation_id": f"cid-{day}",
        "recorded_on": day,
        "passed": passed,
        "failure_code": failure_code,
        "total_files": sum(int(str(r["files"])) for r in rows),
        "runtime_files": sum(int(str(r["runtime_files"])) for r in rows),
        "repositories": list(rows),
    }


def _census_printing(record: dict[str, object], code: int):  # type: ignore[no-untyped-def]
    def run(argv: Sequence[str], *, environ: Mapping[str, str] | None = None) -> int:
        print("CENSUS-RECORD " + json.dumps(record))
        return code

    return run


def _tick(
    tmp_path: Path,
    record: dict[str, object],
    code: int,
    publisher: FakePublisher,
) -> int:
    return tick.main(
        ["--omni-home", str(tmp_path), "--state-dir", str(tmp_path / "state")],
        environ={},
        stdout=io.StringIO(),
        publisher=publisher,
        census=_census_printing(record, code),
    )


YESTERDAY = _record(
    "2026-11-23", [_row("omnimarket", 131, 20), _row("omnibase_infra", 23, 6)]
)
TODAY = _record(
    "2026-11-24", [_row("omnimarket", 128, 17), _row("omnibase_infra", 21, 5)]
)


def test_daily_record_event_carries_counts_runtime_count_and_change_against_previous_day(
    tmp_path: Path,
) -> None:
    publisher = FakePublisher()
    assert _tick(tmp_path, YESTERDAY, 0, publisher) == 0
    assert _tick(tmp_path, TODAY, 0, publisher) == 0
    assert len(publisher.sent) == 2
    sent = publisher.sent[1]
    assert sent["topic"] == TOPIC
    payload = sent["payload"]
    assert isinstance(payload, dict)
    assert payload["record"]["total_files"] == 149
    assert payload["record"]["runtime_files"] == 22
    assert [r["repository"] for r in payload["record"]["repositories"]] == [
        "omnimarket",
        "omnibase_infra",
    ]
    change = payload["change"]
    assert change["basis"] == "previous-day"
    assert change["previous_recorded_on"] == "2026-11-23"
    assert change["total_files_change"] == -5
    assert change["runtime_files_change"] == -4
    by_repo = {r["repository"]: r for r in change["repositories"]}
    assert by_repo["omnimarket"]["files_change"] == -3
    assert by_repo["omnibase_infra"]["files_change"] == -2
    assert payload["partial"] is False
    assert sent["correlation_id"] == TODAY["correlation_id"]


def test_daily_record_first_run_states_no_previous_record_and_emits_no_change(
    tmp_path: Path,
) -> None:
    publisher = FakePublisher()
    assert _tick(tmp_path, TODAY, 0, publisher) == 0
    payload = publisher.sent[0]["payload"]
    assert isinstance(payload, dict)
    assert payload["record"]["total_files"] == 149
    change = payload["change"]
    assert change["basis"] == "no-previous-record"
    assert change["previous_recorded_on"] is None
    assert change["total_files_change"] is None
    assert change["runtime_files_change"] is None
    assert change["repositories"] == []


def test_daily_record_second_run_the_same_day_compares_with_the_previous_day_not_itself(
    tmp_path: Path,
) -> None:
    publisher = FakePublisher()
    _tick(tmp_path, YESTERDAY, 0, publisher)
    _tick(tmp_path, TODAY, 0, publisher)
    _tick(tmp_path, TODAY, 0, publisher)
    payload = publisher.sent[2]["payload"]
    assert isinstance(payload, dict)
    assert payload["change"]["previous_recorded_on"] == "2026-11-23"
    assert payload["change"]["total_files_change"] == -5


def test_daily_record_a_count_that_grew_still_fails_the_run_and_is_published(
    tmp_path: Path,
) -> None:
    grew = _record(
        "2026-11-24",
        [
            _row("omnimarket", 133, 20, ratchet=131, state="grew"),
            _row("omnibase_infra", 23, 6),
        ],
        passed=False,
        failure_code="ratchet-grew",
    )
    publisher = FakePublisher()
    _tick(tmp_path, YESTERDAY, 0, publisher)
    assert _tick(tmp_path, grew, 1, publisher) == 1
    payload = publisher.sent[1]["payload"]
    assert isinstance(payload, dict)
    assert payload["record"]["passed"] is False
    assert payload["record"]["failure_code"] == "ratchet-grew"
    assert payload["change"]["total_files_change"] == 2
    assert payload["partial"] is False


def test_daily_record_a_missing_repository_is_a_typed_failure_and_publishes_the_partial_record(
    tmp_path: Path,
) -> None:
    partial = _record(
        "2026-11-24",
        [
            _row("omnibase_compat", 0, 0, ratchet=0, state="absent"),
            _row("omnimarket", 128, 17),
            _row("omnibase_infra", 21, 5),
        ],
        passed=False,
        failure_code="repo-absent",
    )
    previous = _record(
        "2026-11-23",
        [
            _row("omnibase_compat", 0, 0),
            _row("omnimarket", 131, 20),
            _row("omnibase_infra", 23, 6),
        ],
    )
    publisher = FakePublisher()
    _tick(tmp_path, previous, 0, publisher)
    assert _tick(tmp_path, partial, 69, publisher) == 69
    payload = publisher.sent[1]["payload"]
    assert isinstance(payload, dict)
    assert payload["partial"] is True
    assert payload["absent_repositories"] == ["omnibase_compat"]
    assert payload["record"]["failure_code"] == "repo-absent"
    change = payload["change"]
    # the change is read over the repositories counted on both days, never over the absent one
    assert change["compared_repositories"] == ["omnimarket", "omnibase_infra"]
    assert change["skipped_repositories"] == ["omnibase_compat"]
    assert change["total_files_change"] == -5


def test_daily_record_a_missing_publish_topic_default_fails_before_the_census_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    contract = yaml.safe_load((PACKAGE / "contract.yaml").read_text(encoding="utf-8"))
    contract["event_bus"]["publish_topics"] = []
    declared = tmp_path / "contract.yaml"
    declared.write_text(yaml.safe_dump(contract), encoding="utf-8")
    monkeypatch.setattr(tick, "CONTRACT_FILE", declared)
    ran: list[bool] = []

    def census(argv: Sequence[str], *, environ: Mapping[str, str] | None = None) -> int:
        ran.append(True)
        return 0

    publisher = FakePublisher()
    err = io.StringIO()
    monkeypatch.setattr("sys.stderr", err)
    code = tick.main(
        ["--omni-home", str(tmp_path), "--state-dir", str(tmp_path / "state")],
        environ={},
        stdout=io.StringIO(),
        publisher=publisher,
        census=census,
    )
    assert code == 3
    assert ran == []
    assert publisher.sent == []
    assert "publish topic" in err.getvalue()
    assert not (tmp_path / "state" / "last-run.json").exists()


def test_daily_record_a_refused_publish_fails_the_unit_and_still_keeps_the_record(
    tmp_path: Path,
) -> None:
    publisher = FakePublisher(accept=False)
    assert _tick(tmp_path, TODAY, 0, publisher) == 3
    evidence = json.loads((tmp_path / "state" / "last-run.json").read_text("utf-8"))
    assert evidence["record"]["total_files"] == 149
    assert evidence["published"] is False


def test_daily_record_the_published_topic_is_declared_in_the_contract() -> None:
    contract = yaml.safe_load((PACKAGE / "contract.yaml").read_text(encoding="utf-8"))
    assert contract["event_bus"]["publish_topics"] == [TOPIC]


def test_daily_record_packaged_overlay_holds_the_history_settings() -> None:
    overlay = yaml.safe_load((PACKAGE / "overlay.example.yaml").read_text("utf-8"))
    assert overlay["daily_history_file"] == "daily-records.json"
    assert overlay["daily_history_days"] == 35
