"""The adoption recovery effect reads the adoption journal and starts one detached run per grace window."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from omnimarket.nodes.node_adoption_recovery_effect.handler import (
    ADOPT_FIX,
    ADOPT_START_MARK_FILE,
    HandlerAdoptionRecovery,
)
from omnimarket.nodes.node_adoption_recovery_effect.models import (
    ModelAdoptionRecoveryRequest,
)

NOW = datetime(2026, 10, 20, 12, 0, tzinfo=UTC)


def journal(root: Path, *minutes_ago: float) -> Path:
    path = root / "adoption" / "events.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(
            json.dumps(
                {"ts": (NOW - timedelta(minutes=m)).strftime("%Y-%m-%dT%H:%M:%SZ")}
            )
            + "\n"
            for m in minutes_ago
        )
    )
    return path.parent / "state.json"


class Starter:
    def __init__(self, error: str | None = None) -> None:
        self.calls: list[Path] = []
        self.error = error

    def __call__(self, log: Path) -> str | None:
        self.calls.append(log)
        return self.error


def recover(tmp_path: Path, env: dict[str, str], starter: Starter):
    request = ModelAdoptionRecoveryRequest(NOW, env, tmp_path / "state")
    return HandlerAdoptionRecovery(starter).handle(request)


def test_a_fresh_journal_finds_nothing_and_starts_nothing(tmp_path: Path) -> None:
    state = journal(tmp_path, 5)
    starter = Starter()
    assert recover(tmp_path, {"ONEX_ADOPTION_STATE": str(state)}, starter) == []
    assert starter.calls == []


def test_a_stale_journal_starts_one_run_and_records_the_start_mark(
    tmp_path: Path,
) -> None:
    state = journal(tmp_path, 240)
    starter = Starter()
    found = recover(tmp_path, {"ONEX_ADOPTION_STATE": str(state)}, starter)
    assert [(level, loop, fix) for level, loop, _why, fix in found] == [
        ("missing", "adoption", ADOPT_FIX)
    ]
    assert "240m old" in found[0][2]
    assert len(starter.calls) == 1
    assert (
        json.loads((tmp_path / "state" / ADOPT_START_MARK_FILE).read_text())["started"]
        == "2026-10-20T12:00:00Z"
    )


def test_a_start_inside_the_grace_window_is_not_repeated(tmp_path: Path) -> None:
    state = journal(tmp_path, 240)
    (tmp_path / "state").mkdir()
    (tmp_path / "state" / ADOPT_START_MARK_FILE).write_text(
        json.dumps({"started": "2026-10-20T11:30:00Z"})
    )
    starter = Starter()
    found = recover(tmp_path, {"ONEX_ADOPTION_STATE": str(state)}, starter)
    assert "has written no event yet" in found[0][2]
    assert starter.calls == []


def test_a_start_that_fails_says_why(tmp_path: Path) -> None:
    state = journal(tmp_path, 240)
    found = recover(tmp_path, {"ONEX_ADOPTION_STATE": str(state)}, Starter("no script"))
    assert "could not start adopt_merged.py run: no script" in found[0][2]


def test_the_switch_off_value_judges_nothing(tmp_path: Path) -> None:
    starter = Starter()
    assert recover(tmp_path, {"ONEX_ADOPT_ON_TICK": "0"}, starter) == []
    assert starter.calls == []


def test_no_state_path_is_one_unknown_naming_the_registry_root_variable(
    tmp_path: Path,
) -> None:
    found = recover(tmp_path, {}, Starter())
    assert found[0][0] == "unknown"
    assert "ONEX_REGISTRY_ROOT" in found[0][2]


def test_the_registry_root_locates_the_adoption_state(tmp_path: Path) -> None:
    root = tmp_path / "registry"
    events = root / ".claude_scratch" / "adoption" / "events.jsonl"
    events.parent.mkdir(parents=True)
    events.write_text(
        json.dumps({"ts": (NOW - timedelta(minutes=3)).strftime("%Y-%m-%dT%H:%M:%SZ")})
        + "\n"
    )
    assert recover(tmp_path, {"ONEX_REGISTRY_ROOT": str(root)}, Starter()) == []
