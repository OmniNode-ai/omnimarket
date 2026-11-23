# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One refused overlay never starves the ids after it, and a stale id alarms by name with an owner.

The tick loads the ids its overlay lists in order. A refusal on the second of three ids used to end the run:
the third was never published and nothing named the second. These tests run the tick through its real loader
(only the bus is a fake) over a three-id overlay home whose second id has no pins line.
"""

from __future__ import annotations

import faulthandler
import json
import os
import subprocess
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import yaml
from omnibase_core.validators.no_unguarded_git_subprocess import scrub_git_location_env

from omnimarket.nodes.node_overlay_store_load_effect import (
    runtime_overlay_store_load as load_module,
)
from omnimarket.nodes.node_overlay_store_load_effect import (
    runtime_overlay_store_load_tick as tick,
)
from tests.unit.nodes.node_overlay_store.support import LOADED_TOPIC, FakeBus

pytestmark = pytest.mark.unit

NOW = datetime(2026, 11, 23, 21, 0, tzinfo=UTC)
IDS = ["first_one", "second_one", "third_one"]
MODEL = "omnimarket.models.node_overlays.model_lane_time_box_overlay:ModelLaneTimeBoxOverlay"
OVERLAY = f"""\
bus: inmemory
loaded_by: node_overlay_store_load_effect
clone_relpath: overlays
clone_branch: main
overlay_ids: [{", ".join(IDS)}]
"""


def git(path: Path, *argv: str) -> str:
    return subprocess.run(
        [
            "git",
            "-C",
            str(path),
            "-c",
            "user.name=t",
            "-c",
            "user.email=t@example.com",
            *argv,
        ],
        env=scrub_git_location_env(os.environ),
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


@pytest.fixture(autouse=True)
def _tick_deadline_does_not_outlive_its_test() -> Iterator[None]:
    yield
    faulthandler.cancel_dump_traceback_later()


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A registry root whose ``overlays`` clone sits at origin/main and lists three ids; the second has no pins line."""
    root = tmp_path / "omni"
    clone = root / "overlays"
    own = clone / "overlays" / "node_overlay_store_load_effect"
    own.mkdir(parents=True)
    (own / "overlay.yaml").write_text(OVERLAY, encoding="utf-8")
    for overlay_id in IDS:
        folder = clone / "overlays" / overlay_id
        folder.mkdir(parents=True)
        (folder / "overlay.yaml").write_text(
            yaml.safe_dump({"schema_version": 1, "default_timeout_min": 90}),
            encoding="utf-8",
        )
    pins = "".join(f"{i} 0000 {MODEL}\n" for i in IDS if i != "second_one")
    (clone / ".omnimarket-node-pins").write_text(pins, encoding="utf-8")
    git(clone, "init", "-q", "-b", "main")
    git(clone, "add", "-A")
    git(clone, "commit", "-q", "-m", "x")
    git(clone, "update-ref", "refs/remotes/origin/main", "HEAD")
    monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", str(clone / "overlays"))
    return root


@pytest.fixture
def bus(monkeypatch: pytest.MonkeyPatch) -> FakeBus:
    """The real loader runs; only the bus it opens is a fake, and the clock is the test's."""
    fake = FakeBus()

    @asynccontextmanager
    async def opened(**_: Any) -> AsyncIterator[FakeBus]:
        yield fake

    monkeypatch.setattr(load_module, "open_lane_bus", opened)
    monkeypatch.setattr(tick, "_utc_now", lambda: NOW)
    return fake


def seed(state: Path, home: Path, asserted_at: dict[str, datetime]) -> None:
    """A state dir whose ids were last asserted at the given times, all from the clone's current head."""
    head = git(home / "overlays", "rev-parse", "HEAD")
    state.mkdir(parents=True, exist_ok=True)
    (state / "last-load.json").write_text(
        json.dumps(
            {
                i: {
                    "source_ref": f"{i}@{head}",
                    "asserted_at": at.isoformat(),
                    "digest": "d" * 64,
                }
                for i, at in asserted_at.items()
            }
        ),
        encoding="utf-8",
    )


def published_ids(bus: FakeBus) -> list[str]:
    return [
        (key or b"").decode()
        for topic, key, _ in bus.published
        if topic == LOADED_TOPIC
    ]


def alarms(bus: FakeBus) -> list[dict[str, Any]]:
    return [
        json.loads(value)
        for topic, _, value in bus.published
        if topic != LOADED_TOPIC and "assertion-stale" in topic
    ]


def run_tick(home: Path, state: Path) -> int:
    return tick.main(["--omni-home", str(home), "--state-dir", str(state)])


def test_a_refusal_on_the_second_id_does_not_stop_the_third(
    home: Path, bus: FakeBus, tmp_path: Path
) -> None:
    state = tmp_path / "state"
    code = run_tick(home, state)
    assert published_ids(bus) == ["first_one", "third_one"]
    assert code == tick.EXIT_REFUSED
    record = json.loads((state / "last-run.json").read_text(encoding="utf-8"))
    assert sorted(record["refused_ids"]) == ["second_one"]
    assert "second_one" in record["refused"]
    assert [row["overlay_id"] for row in record["loaded"]] == [
        "first_one",
        "third_one",
    ]


def test_the_alarm_names_the_second_id_only_and_an_owner(
    home: Path, bus: FakeBus, tmp_path: Path
) -> None:
    state = tmp_path / "state"
    seed(state, home, {i: NOW - timedelta(hours=3) for i in IDS})
    run_tick(home, state)
    raised = alarms(bus)
    assert [a["overlay_id"] for a in raised] == ["second_one"]
    assert raised[0]["owner"]
    assert raised[0]["age_s"] > 7200
    # the ids that loaded this run carry a fresh assertion in the state dir, the refused one keeps its old time
    held = json.loads((state / "last-load.json").read_text(encoding="utf-8"))
    assert held["first_one"]["asserted_at"] == NOW.isoformat()
    assert held["third_one"]["asserted_at"] == NOW.isoformat()
    assert held["second_one"]["asserted_at"] == (NOW - timedelta(hours=3)).isoformat()


def test_a_refusal_inside_the_two_hours_publishes_the_rest_without_an_alarm(
    home: Path, bus: FakeBus, tmp_path: Path
) -> None:
    state = tmp_path / "state"
    seed(state, home, {i: NOW - timedelta(minutes=70) for i in IDS})
    run_tick(home, state)
    assert published_ids(bus) == ["first_one", "third_one"]
    assert alarms(bus) == []


def test_an_unchanged_clone_asserts_each_overdue_id_again_within_the_hour(
    home: Path, bus: FakeBus, tmp_path: Path
) -> None:
    state = tmp_path / "state"
    seed(
        state,
        home,
        {
            "first_one": NOW - timedelta(minutes=61),
            "second_one": NOW - timedelta(minutes=10),
            "third_one": NOW - timedelta(minutes=10),
        },
    )
    run_tick(home, state)
    # only the id past the hour is asserted again; the ones asserted ten minutes ago are not republished
    assert published_ids(bus) == ["first_one"]
    held = json.loads((state / "last-load.json").read_text(encoding="utf-8"))
    assert held["first_one"]["asserted_at"] == NOW.isoformat()
    assert held["third_one"]["asserted_at"] == (NOW - timedelta(minutes=10)).isoformat()
