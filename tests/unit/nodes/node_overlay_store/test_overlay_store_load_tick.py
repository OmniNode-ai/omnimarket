# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The loader's hourly tick (OMN-23353): golden when the clone is at the head of main, error chain for each stale clone."""

from __future__ import annotations

import faulthandler
import json
import os
import subprocess
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from omnibase_core.validators.no_unguarded_git_subprocess import scrub_git_location_env

from omnimarket.models.overlay_store import ModelOverlayStoreLoadResult
from omnimarket.nodes.node_overlay_store_load_effect import (
    runtime_overlay_store_load_tick as tick,
)
from tests.unit.nodes.node_overlay_store.support import LOADED_TOPIC

pytestmark = pytest.mark.unit

OVERLAY = """\
bus: inmemory
loaded_by: node_overlay_store_load_effect
clone_relpath: overlays
clone_branch: main
overlay_ids: [lab_hosts]
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
    """The tick arms a process-exiting deadline; a test running it in-process must disarm it."""
    yield
    faulthandler.cancel_dump_traceback_later()


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A registry root whose ``overlays`` clone sits at origin/main, and the loader's own overlay found in it."""
    root = tmp_path / "omni"
    clone = root / "overlays"
    (clone / "overlays" / "node_overlay_store_load_effect").mkdir(parents=True)
    (clone / "overlays" / "node_overlay_store_load_effect" / "overlay.yaml").write_text(
        OVERLAY, encoding="utf-8"
    )
    git(clone, "init", "-q", "-b", "main")
    git(clone, "add", "-A")
    git(clone, "commit", "-q", "-m", "x")
    git(clone, "update-ref", "refs/remotes/origin/main", "HEAD")
    monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", str(clone / "overlays"))
    return root


def fake_run(loaded: list[str]) -> Callable[..., Any]:
    async def run(
        overlay: Any, clone: Path, ids: list[str], bootstrap: Any
    ) -> list[Any]:
        loaded.extend(ids)
        head = git(clone, "rev-parse", "HEAD")
        return [
            ModelOverlayStoreLoadResult(
                overlay_id=i,
                digest="d" * 64,
                source_ref=f"{i}@{head}",
                topic=LOADED_TOPIC,
                loaded_at=datetime(2026, 11, 9, tzinfo=UTC),
            )
            for i in ids
        ]

    return run


def last_run(state: Path) -> dict[str, Any]:
    return json.loads((state / "last-run.json").read_text(encoding="utf-8"))


def test_golden_a_clone_at_the_head_of_main_loads_every_named_overlay(
    home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    loaded: list[str] = []
    monkeypatch.setattr(tick, "_run", fake_run(loaded))
    state = tmp_path / "state"
    assert tick.main(["--omni-home", str(home), "--state-dir", str(state)]) == 0
    assert loaded == ["lab_hosts"]
    record = last_run(state)
    head = git(home / "overlays", "rev-parse", "HEAD")
    assert record["refused"] == ""
    assert record["loaded"][0]["source_ref"] == f"lab_hosts@{head}"


def test_error_a_clone_behind_origin_main_loads_nothing_and_says_which_head_it_holds(
    home: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clone = home / "overlays"
    git(clone, "commit", "-q", "--allow-empty", "-m", "side")
    git(clone, "checkout", "-q", "-b", "OMN-22993-h051-shutdown")
    loaded: list[str] = []
    monkeypatch.setattr(tick, "_run", fake_run(loaded))
    state = tmp_path / "state"
    code = tick.main(["--omni-home", str(home), "--state-dir", str(state)])
    assert code == tick.EXIT_STALE_CLONE
    assert loaded == []
    record = last_run(state)
    assert "not at origin/main" in record["refused"]
    assert record["head"] != record["remote_head"]
    assert record["loaded"] == []


def test_error_a_clone_with_uncommitted_changes_loads_nothing(
    home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    changed = (
        home
        / "overlays"
        / "overlays"
        / "node_overlay_store_load_effect"
        / "overlay.yaml"
    )
    changed.write_text(OVERLAY + "# edited\n", encoding="utf-8")
    loaded: list[str] = []
    monkeypatch.setattr(tick, "_run", fake_run(loaded))
    code = tick.main(["--omni-home", str(home), "--state-dir", str(tmp_path / "state")])
    assert code == tick.EXIT_STALE_CLONE
    assert loaded == []


def test_error_an_overlay_naming_no_clone_branch_or_ids_is_refused(
    home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = (
        home
        / "overlays"
        / "overlays"
        / "node_overlay_store_load_effect"
        / "overlay.yaml"
    )
    path.write_text(
        "bus: inmemory\nloaded_by: node_overlay_store_load_effect\n", encoding="utf-8"
    )
    loaded: list[str] = []
    monkeypatch.setattr(tick, "_run", fake_run(loaded))
    code = tick.main(["--omni-home", str(home), "--state-dir", str(tmp_path / "state")])
    assert code == tick.EXIT_REFUSED
    assert loaded == []
