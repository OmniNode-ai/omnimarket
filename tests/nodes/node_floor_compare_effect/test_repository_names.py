# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Each repository is one row of the compare, under its registry name (OMN-23134, AC1 and AC2).

The repositories here are stand-ins: ``foo_bar`` is a registry clone whose repository is spelled ``foo-bar`` (an underscore
and a hyphen spelling), ``delta`` and ``delta-new`` are the old and the new name of one renamed repository. The failing
control of each test is a named injected break: the overlay that maps nothing and has no registry, which reads as the
compare did before the fix, with one row per spelling.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from datetime import datetime, timedelta
from pathlib import Path

import pytest
import yaml

from omnimarket.handlers.emission_sink import InMemoryEmissionSink
from omnimarket.nodes.node_floor_compare_effect.handlers.adapter_floor_sources import (
    scrub_git_location_env,
)
from omnimarket.nodes.node_floor_compare_effect.handlers.adapter_repository_names import (
    DEFAULT_OVERLAY,
    OVERLAY_ENV,
    canonical_snapshot,
    load_repository_names,
    unmapped_spellings,
)
from omnimarket.nodes.node_floor_compare_effect.handlers.handler_floor_compare import (
    HandlerFloorCompare,
    build_comparison,
)
from omnimarket.nodes.node_floor_compare_effect.models.model_floor_compare import (
    ModelFloorCompareRequest,
    ModelFloorComparison,
    ModelSnapshot,
)
from omnimarket.nodes.node_floor_compare_effect.models.model_repository_names import (
    ModelRepositoryNames,
)
from tests.nodes.node_floor_compare_effect.support import (
    NOW,
    RT,
    build_home,
    seed,
    snap,
)

pytestmark = pytest.mark.unit

FAMILY = {"prefix": "git_merges:", "total_key": "git_merges_total"}
RENAMED = {
    "canonical": "delta",
    "aliases": ["delta-new"],
    "evidence": "one repository renamed",
}


def write_overlay(
    path: Path, repositories: list[dict], registry: str | None = None
) -> Path:
    doc: dict = {"families": [FAMILY], "repositories": repositories}
    if registry:
        doc["registry_relpath"] = registry
    path.write_text(yaml.safe_dump({"repository_names": doc}), encoding="utf-8")
    return path


def write_registry(root: Path, entries: dict[str, str]) -> None:
    (root / "registry_root").mkdir(parents=True, exist_ok=True)
    (root / "registry_root" / "repos.yaml").write_text(
        yaml.safe_dump(
            {
                "repositories": [
                    {"name": name, "repo": f"Org/{repo}"}
                    for name, repo in entries.items()
                ]
            }
        ),
        encoding="utf-8",
    )


def names_for(tmp_path: Path, repositories: list[dict]) -> ModelRepositoryNames:
    """The overlay with these entries and a registry that spells ``foo_bar`` as the repository ``foo-bar``."""
    write_registry(tmp_path, {"foo_bar": "foo-bar", "alpha": "alpha", "omnx": "omnx"})
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    overlay = write_overlay(
        tmp_path / "names.yaml", repositories, "../registry_root/repos.yaml"
    )
    names, notes = load_repository_names({OVERLAY_ENV: str(overlay)}, home, "absent")
    assert notes == ()
    return names


def maps_nothing(tmp_path: Path) -> Path:
    return write_overlay(tmp_path / "maps-nothing.yaml", [])


def recorded(values: dict[str, float], days_ago: float = 0) -> ModelSnapshot:
    return snap(days_ago, values)


THEN = {
    "git_merges:foo_bar": 3.0,
    "git_merges:foo-bar": 3.0,
    "git_merges:delta": 7.0,
    "git_merges:delta-new": 7.0,
    "git_merges_total": 20.0,
}
NOW_VALUES = {
    "git_merges:foo_bar": 6.0,
    "git_merges:foo-bar": 6.0,
    "git_merges:delta": 1.0,
    "git_merges:delta-new": 1.0,
    "git_merges_total": 14.0,
}


def day_lines(names: ModelRepositoryNames, then: dict, now: dict) -> list:
    comparison = build_comparison(
        (canonical_snapshot(recorded(then, 1), names),),
        canonical_snapshot(recorded(now), names),
        RT,
    )
    day = comparison.windows[0]
    return [*day.better, *day.worse, *day.unread]


def test_the_registry_clone_list_supplies_the_underscore_and_hyphen_alias(
    tmp_path: Path,
) -> None:
    names = names_for(tmp_path, [RENAMED])
    got = {e.canonical: set(e.aliases) for e in names.repositories}
    assert got == {"delta": {"delta-new"}, "foo_bar": {"foo-bar"}}


def test_recorded_rows_naming_both_spellings_land_in_one_row(tmp_path: Path) -> None:
    lines = day_lines(names_for(tmp_path, [RENAMED]), THEN, NOW_VALUES)
    keys = [ln.key for ln in lines]
    assert keys.count("git_merges:foo_bar") == 1
    assert "git_merges:foo-bar" not in keys
    assert keys.count("git_merges:delta") == 1
    assert "git_merges:delta-new" not in keys
    foo = next(ln for ln in lines if ln.key == "git_merges:foo_bar")
    assert (foo.old, foo.new, foo.verdict) == (3.0, 6.0, "better")
    delta = next(ln for ln in lines if ln.key == "git_merges:delta")
    assert (delta.old, delta.new, delta.verdict) == (7.0, 1.0, "worse")


def test_failing_control_with_nothing_mapped_each_spelling_is_a_row_of_its_own(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    overlay = maps_nothing(tmp_path)
    names, _ = load_repository_names({OVERLAY_ENV: str(overlay)}, home, "absent")
    keys = [ln.key for ln in day_lines(names, THEN, NOW_VALUES)]
    assert {
        "git_merges:foo_bar",
        "git_merges:foo-bar",
        "git_merges:delta",
        "git_merges:delta-new",
    } <= set(keys)


def test_two_reads_of_one_repository_keep_the_larger_and_count_it_once_in_the_total(
    tmp_path: Path,
) -> None:
    names = names_for(tmp_path, [RENAMED])
    got = canonical_snapshot(
        recorded({**NOW_VALUES, "git_merges:delta-new": 2.0}), names
    )
    assert got.values["git_merges:delta"] == 2.0
    assert got.values["git_merges:foo_bar"] == 6.0
    assert "git_merges:delta-new" not in got.values
    assert "git_merges:foo-bar" not in got.values
    # The recorded total of 14 counted both reads of each repository; it drops the smaller read of each: 14 - 6 - 1.
    assert got.values["git_merges_total"] == 7.0


def test_the_total_drops_each_repeat_and_leaves_other_repositories(
    tmp_path: Path,
) -> None:
    names = names_for(tmp_path, [RENAMED])
    got = canonical_snapshot(
        recorded(
            {
                "git_merges:foo_bar": 6.0,
                "git_merges:foo-bar": 6.0,
                "git_merges:alpha": 14.0,
                "git_merges_total": 26.0,
            }
        ),
        names,
    )
    assert got.values["git_merges_total"] == 20.0
    assert got.values["git_merges:alpha"] == 14.0


def test_an_old_name_early_and_a_new_name_late_read_as_one_series(
    tmp_path: Path,
) -> None:
    names = names_for(tmp_path, [RENAMED])
    history = (
        canonical_snapshot(snap(7, {"git_merges:delta": 4.0}), names),
        canonical_snapshot(snap(1, {"git_merges:delta": 5.0}), names),
    )
    today = canonical_snapshot(snap(0, {"git_merges:delta-new": 9.0}), names)
    week = build_comparison(history, today, RT).windows[1]
    line = next(ln for ln in (*week.better, *week.worse) if "delta" in ln.key)
    assert (line.key, line.old, line.new) == ("git_merges:delta", 4.0, 9.0)


def test_the_map_is_overlay_data_not_code(tmp_path: Path) -> None:
    other = {"canonical": "omega", "aliases": ["omega-old"], "evidence": "test"}
    names = names_for(tmp_path, [other])
    got = canonical_snapshot(
        recorded({"git_merges:omega-old": 2.0, "git_merges:delta-new": 1.0}), names
    )
    assert got.values == {"git_merges:omega": 2.0, "git_merges:delta-new": 1.0}


def test_a_spelling_neither_source_joins_is_named_in_a_note(tmp_path: Path) -> None:
    names = names_for(tmp_path, [])
    notes = unmapped_spellings(
        recorded({"git_merges:qux_zip": 1.0, "git_merges:qux-zip": 1.0}), names
    )
    assert len(notes) == 1
    assert "qux-zip" in notes[0]
    assert "qux_zip" in notes[0]
    assert (
        unmapped_spellings(recorded({"git_merges:a": 1.0, "git_merges:b": 1.0}), names)
        == ()
    )


def test_a_spelling_listed_under_two_repositories_is_refused() -> None:
    twice = {"canonical": "c", "aliases": ["b"], "evidence": "x"}
    with pytest.raises(ValueError, match="listed twice"):
        ModelRepositoryNames.model_validate(
            {
                "families": [FAMILY],
                "repositories": [
                    {"canonical": "a", "aliases": ["b"], "evidence": "x"},
                    twice,
                ],
            }
        )
    with pytest.raises(ValueError, match="listed twice"):
        ModelRepositoryNames.model_validate(
            {
                "families": [FAMILY],
                "repositories": [{"canonical": "a", "aliases": ["a"], "evidence": "x"}],
            }
        )


def test_the_overlay_the_overlay_entry_wins_over_the_registry(tmp_path: Path) -> None:
    write_registry(tmp_path, {"foo_bar": "foo-bar"})
    home = tmp_path / "home"
    home.mkdir()
    entry = {"canonical": "foo-bar", "aliases": ["foo_bar"], "evidence": "x"}
    overlay = write_overlay(tmp_path / "n.yaml", [entry], "../registry_root/repos.yaml")
    names, _ = load_repository_names({OVERLAY_ENV: str(overlay)}, home, "absent")
    assert [(e.canonical, e.aliases) for e in names.repositories] == [
        ("foo-bar", ("foo_bar",))
    ]


def test_the_overlay_is_the_env_file_then_the_hosts_own_then_the_shipped_one(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    (home / "state").mkdir(parents=True)
    host = write_overlay(home / "state" / "names.yaml", [RENAMED])
    named = write_overlay(
        tmp_path / "named.yaml",
        [{"canonical": "omega", "aliases": ["omega-old"], "evidence": "x"}],
    )
    shipped, _ = load_repository_names({}, home, "state/absent.yaml")
    assert shipped.repositories == ()
    assert shipped == load_repository_names({}, home, "state/absent.yaml")[0]
    assert DEFAULT_OVERLAY.is_file()
    own, _ = load_repository_names({}, home, "state/names.yaml")
    assert [e.canonical for e in own.repositories] == ["delta"]
    env, _ = load_repository_names({OVERLAY_ENV: str(named)}, home, "state/names.yaml")
    assert [e.canonical for e in env.repositories] == ["omega"]
    assert host.is_file()


def test_a_registry_clone_list_that_cannot_be_read_is_a_note_not_a_failure(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    overlay = write_overlay(tmp_path / "n.yaml", [RENAMED], "../nowhere/repos.yaml")
    names, notes = load_repository_names({OVERLAY_ENV: str(overlay)}, home, "absent")
    assert [e.canonical for e in names.repositories] == ["delta"]
    assert len(notes) == 1
    assert "cannot be read" in notes[0]


IDENTITY = {
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@example.com",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@example.com",
}


def make_clone(path: Path, hours_ago: list[float]) -> None:
    path.mkdir(parents=True)

    def git(*args: str, when: datetime | None = None) -> None:
        dated = (
            {
                "GIT_COMMITTER_DATE": when.isoformat(),
                "GIT_AUTHOR_DATE": when.isoformat(),
            }
            if when is not None
            else {}
        )
        subprocess.run(
            ["git", "-C", str(path), *args],
            check=True,
            capture_output=True,
            env={**scrub_git_location_env(os.environ), **IDENTITY, **dated},
        )

    git("init", "-q", "-b", "main")
    for n, hours in enumerate(hours_ago):
        git(
            "commit",
            "-q",
            "--allow-empty",
            "-m",
            f"merge {n}",
            when=NOW - timedelta(hours=hours),
        )
    git("update-ref", "refs/remotes/origin/main", "HEAD")
    git("symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main")


def served_home(tmp_path: Path) -> Path:
    """A registry home whose canonical clones hold one repository under two directory names (as the floor's host did)."""
    home = build_home(tmp_path)
    write_registry(tmp_path, {"foo_bar": "foo-bar", "omnx": "omnx"})
    make_clone(home / "foo_bar", [2, 3, 4, 5, 6, 7])
    shutil.copytree(home / "foo_bar", home / "foo-bar")
    make_clone(home / "omnx", [1, 2])
    seed(
        home,
        snap(
            1,
            {
                "git_merges:foo_bar": 3.0,
                "git_merges:foo-bar": 3.0,
                "git_merges:omnx": 1.0,
            },
        ),
    )
    return home


def run_cycle(home: Path, sink: InMemoryEmissionSink):
    return HandlerFloorCompare(sink=sink, clock=lambda: NOW).run(
        ModelFloorCompareRequest(home=str(home), now=NOW)
    )


def written(home: Path) -> ModelFloorComparison:
    return ModelFloorComparison.model_validate_json(
        (home / RT.output_relpath).read_text()
    )


def repository_rows(doc: ModelFloorComparison) -> list[tuple[int, str]]:
    """(window days, repository) of every merge row of every group of the comparison."""
    rows: list[tuple[int, str]] = []
    for window in doc.windows:
        for line in (*window.better, *window.worse, *window.unread):
            if line.key.startswith("git_merges:"):
                rows.append((window.days, line.key.removeprefix("git_merges:")))
    return rows


def test_golden_the_served_comparison_shows_each_repository_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(OVERLAY_ENV, raising=False)
    home = served_home(tmp_path)
    host = home / RT.repository_names_overlay_relpath
    host.parent.mkdir(parents=True, exist_ok=True)
    write_overlay(host, [], "../registry_root/repos.yaml")
    sink = InMemoryEmissionSink()
    result = run_cycle(home, sink)
    assert result.ok
    assert not [n for n in result.source_notes if "foo" in n or "repositor" in n]
    assert [e.phase for e in sink.events] == ["start", "success"]
    doc = written(home)
    rows = repository_rows(doc)
    assert rows
    assert len(rows) == len(set(rows))
    assert {r for _, r in rows} == {"foo_bar", "omnx"}
    kb = next(ln for ln in doc.windows[0].better if ln.key == "git_merges:foo_bar")
    assert (kb.old, kb.new) == (3.0, 6.0)
    assert "Merges to foo_bar" in kb.words
    # The history file keeps each source's spelling; only the read is canonical.
    assert "git_merges:foo-bar" in (home / RT.history_relpath).read_text()


def test_failing_control_the_overlay_that_maps_nothing_serves_the_repository_twice(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(OVERLAY_ENV, str(maps_nothing(tmp_path)))
    home = served_home(tmp_path)
    result = run_cycle(home, InMemoryEmissionSink())
    assert result.ok
    assert any("foo-bar" in n for n in result.source_notes)
    spelled = {r for _, r in repository_rows(written(home))}
    assert {"foo_bar", "foo-bar"} <= spelled


def test_the_hosts_own_overlay_is_read_without_an_env_var(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(OVERLAY_ENV, raising=False)
    home = served_home(tmp_path)
    make_clone(home / "delta-new", [1])
    make_clone(home / "delta", [1, 2, 3])
    host = home / RT.repository_names_overlay_relpath
    host.parent.mkdir(parents=True, exist_ok=True)
    write_overlay(host, [RENAMED], "../registry_root/repos.yaml")
    assert run_cycle(home, InMemoryEmissionSink()).ok
    rows = {r for _, r in repository_rows(written(home))}
    assert "delta" in rows
    assert "delta-new" not in rows


def test_error_an_unreadable_overlay_is_a_note_and_the_cycle_still_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(OVERLAY_ENV, str(tmp_path / "absent.yaml"))
    home = served_home(tmp_path)
    sink = InMemoryEmissionSink()
    result = run_cycle(home, sink)
    assert result.ok
    assert [e.phase for e in sink.events] == ["start", "success"]
    assert any(
        "repository names overlay cannot be read" in n for n in result.source_notes
    )
    assert sink.events[1].counters["source_notes"] >= 1
