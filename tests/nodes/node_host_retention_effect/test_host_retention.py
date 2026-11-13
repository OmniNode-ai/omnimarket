# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Host retention, built from the recorded facts of the lab host census of 2026-11-13.

Recorded that day on a lab host: the root disk was 75 percent used with a 99 GiB uv cache, runner work and cache folders
and 40 GiB of lane scratch; an installed unit named a scratch directory as its repository root, so a rule that only
read processes would have removed a tree a unit runs from. Every host, path and number below is fixture data. Each guard
test has a blind-guard control: the same scene with the guard's input emptied removes the entry, which shows the guard
(and not the age rule) is what kept it.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any

import pytest

from omnimarket.nodes.node_host_retention_effect.handlers.handler_host_retention import (
    load_bounds,
)
from omnimarket.nodes.node_host_retention_effect.handlers.host_retention_ports import (
    DockerCiHostRemove,
    DockerCiHostState,
    DockerContainerMounts,
    ProcessUvCache,
)
from omnimarket.nodes.node_temp_retention_effect.handlers.handler_temp_retention import (
    ProcessReferences,
    tree_bytes,
)
from tests.nodes.node_host_retention_effect.host_retention_fakes import (
    CONTAINER_WORK,
    UID,
    UNREADABLE,
    FakeLanes,
    FakeMounts,
    FakeRunnerRemove,
    FakeRunnerState,
    FakeUnits,
    FakeUv,
    entry,
    make_ports,
    run,
    scratch_class,
)

pytestmark = pytest.mark.unit

GIB = 2**30


def scratch(tmp_path: Path) -> Path:
    root = tmp_path / "scratch"
    root.mkdir()
    return root


def kept_reasons(result: Any) -> dict[str, str]:
    return {Path(k.path).name: k.reason for k in result.kept}


# ------------------------------------------------------------------------------------------- age classes


def test_old_entry_is_removed_with_its_bytes_and_a_new_entry_is_kept(
    tmp_path: Path,
) -> None:
    root = scratch(tmp_path)
    old = entry(root, "old", days=10, files=3)
    entry(root, "new", days=1)
    size = tree_bytes(old)
    fields = {"age_classes": [scratch_class(root)]}

    result = run(tmp_path, fields)

    assert result.status == "ok"
    assert [Path(i.path).name for i in result.items] == ["old"]
    assert result.items[0].bytes == size
    assert result.total_bytes == size
    assert not old.exists()
    assert kept_reasons(result) == {"new": "too_new"}
    assert result.totals[0].removed_count == 1
    assert result.totals[0].freed_bytes == size
    assert [(r.used_percent) for r in result.disk_before] == [80.0]
    assert [(r.used_percent) for r in result.disk_after] == [50.0]
    assert run(tmp_path, fields).items == ()


def test_dry_run_lists_what_a_real_run_removes_and_removes_nothing(
    tmp_path: Path,
) -> None:
    root = scratch(tmp_path)
    old = entry(root, "old", days=10, files=2)
    fields = {"age_classes": [scratch_class(root)]}

    dry = run(tmp_path, fields, dry_run=True)

    assert old.exists()
    assert dry.dry_run is True
    assert dry.disk_after == ()
    assert dry.totals[0].would_remove_count == 1
    assert dry.totals[0].removed_count == 0
    real = run(tmp_path, fields)
    assert not old.exists()
    assert [(i.path, i.bytes) for i in real.items] == [
        (i.path, i.bytes) for i in dry.items
    ]
    assert real.total_bytes == dry.total_bytes


def guard_scene(tmp_path: Path, guard: str, *, blind: bool) -> tuple[Path, Path, Any]:
    root = scratch(tmp_path)
    name = "work-lane-a-1" if guard == "live_lane" else "keep-me"
    keep = entry(root, name, days=10)
    drop = entry(root, "drop-me", days=10)
    refs = ProcessReferences()
    lanes: set[str] = set()
    mounts: set[str] = set()
    units: list[str] = []
    if not blind:
        if guard == "held_by_process":
            refs = ProcessReferences(directories=[keep / "deeper"], blobs=[])
        elif guard == "live_lane":
            lanes = {"lane-a"}
        elif guard == "mounted_by_container":
            mounts = {str(keep / "data")}
        elif guard == "referenced_by_unit":
            units = [f"ExecStart=/usr/bin/python -m x --repo-root {keep}\n"]
    ports = make_ports(
        lanes=FakeLanes(lanes), mounts=FakeMounts(mounts), units=FakeUnits(units)
    )
    return (
        keep,
        drop,
        run(tmp_path, {"age_classes": [scratch_class(root)]}, ports=ports, refs=refs),
    )


GUARDS = ("held_by_process", "live_lane", "mounted_by_container", "referenced_by_unit")


@pytest.mark.parametrize("guard", GUARDS)
def test_a_guarded_entry_is_kept_and_an_unguarded_sibling_is_removed(
    tmp_path: Path, guard: str
) -> None:
    keep, drop, result = guard_scene(tmp_path, guard, blind=False)

    assert keep.exists(), guard
    assert not drop.exists()
    assert kept_reasons(result)[keep.name] == guard


@pytest.mark.parametrize("guard", GUARDS)
def test_control_blind_guard_removes_the_entry(tmp_path: Path, guard: str) -> None:
    keep, drop, result = guard_scene(tmp_path, guard, blind=True)

    assert not keep.exists(), (
        f"{guard}: with its input empty the age rule alone removes the entry"
    )
    assert not drop.exists()
    assert result.errors == ()


def test_foreign_owner_protected_pattern_and_protected_leaf_are_kept(
    tmp_path: Path,
) -> None:
    root = scratch(tmp_path)
    foreign = entry(root, "theirs", days=10)
    fields = {"age_classes": [scratch_class(root)]}
    assert run(tmp_path, fields, uid=UID + 1).items == ()
    assert foreign.exists()

    pattern = entry(root, "claude-1000", days=10)
    leaf = entry(root, "_actions", days=10)
    result = run(
        tmp_path,
        {
            "age_classes": [
                scratch_class(root, protected_name_patterns=["claude-[0-9]+"])
            ]
        },
    )
    assert pattern.exists()
    assert leaf.exists()
    assert not foreign.exists(), "control: the same entry goes for its owner"
    assert kept_reasons(result) == {"claude-1000": "protected", "_actions": "protected"}


def test_an_entry_holding_a_pipe_is_kept_as_a_special_file(tmp_path: Path) -> None:
    root = scratch(tmp_path)
    held = entry(root, "has-pipe", days=10)
    os.mkfifo(held / "pipe")
    os.utime(held / "pipe", (1.0, 1.0), follow_symlinks=False)
    os.utime(held, (1.0, 1.0))

    result = run(tmp_path, {"age_classes": [scratch_class(root)]})

    assert held.exists()
    assert kept_reasons(result) == {"has-pipe": "special_file"}


@pytest.mark.parametrize(
    ("what", "text"),
    [
        ("refs", "process table unreadable"),
        ("lanes", "lane run roots and receipts unreadable"),
        ("mounts", "container mounts unreadable"),
        ("units", "unit files unreadable"),
    ],
)
def test_a_guard_that_cannot_be_read_removes_nothing(
    tmp_path: Path, what: str, text: str
) -> None:
    root = scratch(tmp_path)
    old = entry(root, "old", days=10)
    ports = make_ports(
        lanes=FakeLanes(None if what == "lanes" else set()),
        mounts=FakeMounts(None if what == "mounts" else set()),
        units=FakeUnits(None if what == "units" else []),
    )
    refs = UNREADABLE if what == "refs" else None

    result = run(
        tmp_path, {"age_classes": [scratch_class(root)]}, ports=ports, refs=refs
    )

    assert old.exists()
    assert result.items == ()
    assert text in result.errors


def test_docker_mount_guard_off_does_not_read_mounts(tmp_path: Path) -> None:
    root = scratch(tmp_path)
    old = entry(root, "old", days=10)
    ports = make_ports(mounts=FakeMounts(None))

    result = run(
        tmp_path,
        {"age_classes": [scratch_class(root)], "docker_mount_guard": False},
        ports=ports,
    )

    assert not old.exists()
    assert result.errors == ()


def test_totals_are_kept_per_class(tmp_path: Path) -> None:
    root = scratch(tmp_path)
    results_root = tmp_path / "runs"
    results_root.mkdir()
    entry(root, "a", days=10)
    entry(results_root, "r1", days=10)
    entry(results_root, "r2", days=10)
    entry(results_root, "r3", days=0.5)
    fields = {
        "age_classes": [
            scratch_class(root),
            {
                "name": "results-b",
                "kind": "lane_results",
                "root": str(results_root),
                "keep_days": 7,
            },
        ]
    }

    result = run(tmp_path, fields)

    totals = {t.class_name: t for t in result.totals}
    assert totals["scratch-a"].removed_count == 1
    assert totals["results-b"].removed_count == 2
    assert totals["results-b"].kept_count == 1
    assert totals["results-b"].freed_bytes == sum(
        i.bytes for i in result.items if i.class_name == "results-b"
    )


# ------------------------------------------------------------------------------------------ runner work


def runner_fields(tmp_path: Path, keep_gib: float, **mount: Any) -> dict[str, Any]:
    return {
        "runner_groups": [
            {
                "ids": ["1"],
                "container_template": "ci-runner-{id}",
                "container_user": "runner",
                "mounts": [
                    {
                        "host_root_template": str(
                            tmp_path / "runners" / "{id}" / "work"
                        ),
                        "container_root": CONTAINER_WORK,
                        "disposable_patterns": ["*"],
                        "keep_gib": keep_gib,
                        **mount,
                    }
                ],
            }
        ]
    }


def runner_scene(tmp_path: Path) -> tuple[Path, Path, Path]:
    work = tmp_path / "runners" / "1" / "work"
    work.mkdir(parents=True)
    return (
        entry(work, "big", days=0, files=5),
        entry(work, "mid", days=0, files=2),
        entry(work, "small", days=0),
    )


def test_an_idle_runner_is_evicted_largest_first_down_to_its_budget(
    tmp_path: Path,
) -> None:
    big, mid, small = runner_scene(tmp_path)
    budget = (tree_bytes(mid) + tree_bytes(small) + 1) / GIB
    remover = FakeRunnerRemove(big.parent)

    result = run(
        tmp_path,
        runner_fields(tmp_path, budget),
        ports=make_ports(runner_remove=remover),
    )

    assert not big.exists()
    assert mid.exists()
    assert small.exists()
    assert remover.calls == [("ci-runner-1", f"{CONTAINER_WORK}/big")]
    assert [i.action for i in result.items] == ["evict"]
    assert kept_reasons(result) == {"mid": "under_budget", "small": "under_budget"}
    assert result.totals[0].freed_bytes == result.items[0].bytes


def test_a_dry_run_of_a_runner_evicts_nothing_and_lists_the_same_entries(
    tmp_path: Path,
) -> None:
    big, mid, small = runner_scene(tmp_path)
    remover = FakeRunnerRemove(big.parent)

    result = run(
        tmp_path,
        runner_fields(tmp_path, 0),
        ports=make_ports(runner_remove=remover),
        dry_run=True,
    )

    assert big.exists()
    assert mid.exists()
    assert small.exists()
    assert remover.calls == []
    assert {Path(i.path).name for i in result.items} == {"big", "mid", "small"}


@pytest.mark.parametrize(
    ("idle", "reason"), [(False, "runner_busy"), (None, "runner_state_unknown")]
)
def test_a_runner_that_is_busy_or_unread_is_never_touched(
    tmp_path: Path, idle: bool | None, reason: str
) -> None:
    big, _mid, _small = runner_scene(tmp_path)
    remover = FakeRunnerRemove(big.parent)

    result = run(
        tmp_path,
        runner_fields(tmp_path, 0),
        ports=make_ports(runner_state=FakeRunnerState(idle), runner_remove=remover),
    )

    assert big.exists()
    assert remover.calls == []
    assert [k.reason for k in result.kept] == [reason]


def test_control_an_idle_runner_with_the_same_budget_is_evicted(tmp_path: Path) -> None:
    big, mid, small = runner_scene(tmp_path)
    remover = FakeRunnerRemove(big.parent)

    run(tmp_path, runner_fields(tmp_path, 0), ports=make_ports(runner_remove=remover))

    assert not big.exists()
    assert not mid.exists()
    assert not small.exists()


def test_a_job_that_starts_between_two_removals_stops_the_eviction(
    tmp_path: Path,
) -> None:
    big, mid, small = runner_scene(tmp_path)
    remover = FakeRunnerRemove(big.parent)
    # read 1 sees the runner idle; read 2 (before the first removal) is idle; read 3 (before the second) is busy
    state = FakeRunnerState(True, flips_after=2)

    result = run(
        tmp_path,
        runner_fields(tmp_path, 0),
        ports=make_ports(runner_state=state, runner_remove=remover),
    )

    assert not big.exists()
    assert mid.exists()
    assert small.exists()
    assert "runner_busy" in {k.reason for k in result.kept}


def test_a_failed_removal_is_an_error_and_is_not_counted(tmp_path: Path) -> None:
    big, _mid, _small = runner_scene(tmp_path)

    result = run(
        tmp_path,
        runner_fields(tmp_path, 0),
        ports=make_ports(runner_remove=FakeRunnerRemove(big.parent, fail=True)),
    )

    assert big.exists()
    assert result.items == ()
    assert result.totals[0].removed_count == 0
    assert any("OSError" in e for e in result.errors)


def test_protected_and_unmatched_names_are_never_evicted(tmp_path: Path) -> None:
    work = tmp_path / "runners" / "1" / "work"
    work.mkdir(parents=True)
    protected = entry(work, "_temp", days=0)
    keep = entry(work, "keepme", days=0)
    other = entry(work, "other", days=0)
    fields = runner_fields(
        tmp_path,
        0,
        disposable_patterns=["other", "keepme", "_temp"],
        protected_names=["keepme"],
    )

    result = run(
        tmp_path, fields, ports=make_ports(runner_remove=FakeRunnerRemove(work))
    )

    assert protected.exists()
    assert keep.exists()
    assert not other.exists()
    assert kept_reasons(result)["keepme"] == "protected"
    assert kept_reasons(result)["_temp"] == "protected"


@pytest.mark.parametrize(
    "path",
    [
        "",
        "/",
        "/home",
        "/abs/olute/a/b",
        "a",
        "../x/y",
        "a/../b/c",
        "a//b/c",
        "a/b/../../c",
    ],
)
def test_the_container_remove_adapter_refuses_an_unsafe_path_before_running_anything(
    monkeypatch: pytest.MonkeyPatch, path: str
) -> None:
    def forbidden(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("subprocess must not run")

    monkeypatch.setattr(subprocess, "run", forbidden)

    with pytest.raises(OSError, match=r"is not|has"):
        DockerCiHostRemove(5).remove("c", "runner", path)


def test_the_container_remove_adapter_runs_exactly_one_rm_as_the_runner_user_and_passes_the_path_as_an_argument(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[list[str]] = []

    def fake(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        seen.append(argv)
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(subprocess, "run", fake)

    DockerCiHostRemove(5).remove(
        "ci-runner-1", "runner", f"{CONTAINER_WORK}/big; echo pwned"
    )

    assert seen == [
        [
            "docker",
            "exec",
            "--user",
            "runner",
            "ci-runner-1",
            "sh",
            "-c",
            'rm -rf -- "$HOME/$1"',
            "sh",
            f"{CONTAINER_WORK}/big; echo pwned",
        ]
    ]


# ---------------------------------------------------------------------------------------------- uv cache


def uv_fields(tmp_path: Path, min_gib: float, ci: bool = False) -> dict[str, Any]:
    cache = tmp_path / "uv-cache"
    cache.mkdir(exist_ok=True)
    return {
        "uv_caches": [
            {
                "path": str(cache),
                "uv_binary": "uv",
                "min_gib": min_gib,
                "ci_prune": ci,
                "timeout_seconds": 60,
            }
        ]
    }


def test_a_uv_cache_below_its_minimum_is_not_pruned(tmp_path: Path) -> None:
    uv = FakeUv(size=5 * GIB, after=5 * GIB)

    result = run(tmp_path, uv_fields(tmp_path, 10), ports=make_ports(uv=uv))

    assert uv.prunes == []
    assert [k.reason for k in result.kept] == ["below_min"]


def test_control_a_uv_cache_at_its_minimum_is_pruned_once_and_measured(
    tmp_path: Path,
) -> None:
    uv = FakeUv(size=12 * GIB, after=4 * GIB)

    result = run(tmp_path, uv_fields(tmp_path, 10, ci=True), ports=make_ports(uv=uv))

    assert uv.prunes == [("uv", str(tmp_path / "uv-cache"), True)]
    assert result.items[0].action == "uv_prune"
    assert result.totals[0].freed_bytes == 8 * GIB
    assert result.totals[0].would_remove_bytes == 0
    assert result.total_bytes == 0


def test_a_uv_dry_run_prunes_nothing(tmp_path: Path) -> None:
    uv = FakeUv(size=12 * GIB, after=4 * GIB)

    result = run(
        tmp_path, uv_fields(tmp_path, 10), ports=make_ports(uv=uv), dry_run=True
    )

    assert uv.prunes == []
    assert [i.action for i in result.items] == ["uv_prune"]


def test_a_failed_uv_prune_is_an_error(tmp_path: Path) -> None:
    result = run(
        tmp_path,
        uv_fields(tmp_path, 0),
        ports=make_ports(uv=FakeUv(size=GIB, fail=True)),
    )

    assert any("OSError" in e for e in result.errors)
    assert result.totals[0].removed_count == 0


def test_the_uv_adapter_runs_only_cache_size_and_cache_prune(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[tuple[list[str], str]] = []

    def fake(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        seen.append((argv, kwargs["env"]["UV_CACHE_DIR"]))
        return subprocess.CompletedProcess(argv, 0, "4096\n", "warning: experimental\n")

    monkeypatch.setattr(subprocess, "run", fake)
    adapter = ProcessUvCache()

    assert adapter.size_bytes("uv", "/c/uv-cache", 60) == 4096
    adapter.prune("uv", "/c/uv-cache", False, 60)
    adapter.prune("uv", "/c/uv-cache", True, 60)

    assert [argv for argv, _ in seen] == [
        ["uv", "cache", "size"],
        ["uv", "cache", "prune"],
        ["uv", "cache", "prune", "--ci"],
    ]
    assert {cache for _, cache in seen} == {"/c/uv-cache"}

    monkeypatch.setattr(
        subprocess,
        "run",
        lambda argv, **_kw: subprocess.CompletedProcess(argv, 2, "", "boom"),
    )
    with pytest.raises(OSError, match="boom"):
        adapter.prune("uv", "/c/uv-cache", False, 60)


# --------------------------------------------------------------------------------------------- refusals


def refusal_cases(tmp_path: Path) -> list[tuple[str, dict[str, Any]]]:
    bad_leaf = tmp_path / "elsewhere"
    bad_leaf.mkdir()
    real = scratch(tmp_path)
    link = tmp_path / "link" / "scratch"
    link.parent.mkdir()
    link.symlink_to(real)
    return [
        ("root leaf", {"age_classes": [scratch_class(bad_leaf)]}),
        ("keep_days", {"age_classes": [scratch_class(real, keep_days=0.5)]}),
        ("symlink", {"age_classes": [scratch_class(link)]}),
        ("container root", runner_fields(tmp_path, 1, container_root="etc/passwd.d")),
        (
            "runner id",
            {
                **runner_fields(tmp_path, 1),
                "runner_groups": [
                    {**runner_fields(tmp_path, 1)["runner_groups"][0], "ids": ["a/b"]}
                ],
            },
        ),
        ("unknown field", {"age_classes": [scratch_class(real)], "surprise": 1}),
        ("empty row", {}),
    ]


@pytest.mark.parametrize("case", range(7))
def test_an_unsafe_overlay_is_refused_and_nothing_is_removed(
    tmp_path: Path, case: int
) -> None:
    cases = refusal_cases(tmp_path)
    old = entry(tmp_path / "scratch", "old", days=10)
    name, fields = cases[case]

    result = run(tmp_path, fields)

    assert result.status == "refused", name
    assert result.refusals
    assert result.items == ()
    assert old.exists()


def test_a_host_the_overlay_does_not_declare_is_refused(tmp_path: Path) -> None:
    from omnimarket.nodes.node_host_retention_effect.handlers.handler_host_retention import (
        HandlerHostRetention,
    )
    from omnimarket.nodes.node_host_retention_effect.models.model_host_retention import (
        ModelHostRetentionRequest,
    )
    from tests.nodes.node_host_retention_effect.host_retention_fakes import overlay_file

    root = scratch(tmp_path)
    result = HandlerHostRetention(ports=make_ports()).handle(
        ModelHostRetentionRequest(
            host_name="pool-z",
            overlay_file=overlay_file(tmp_path, age_classes=[scratch_class(root)]),
        )
    )

    assert result.status == "refused"
    assert "pool-z" in result.refusals[0]


def test_the_contract_bounds_are_what_the_tests_assume() -> None:
    bounds = load_bounds()
    assert bounds.min_keep_days == 1
    assert "scratch" in bounds.root_leaf_names["lane_scratch"]
    assert "actions-runner/_work" in bounds.allowed_container_roots


def test_a_command_that_times_out_or_is_missing_is_an_unreadable_answer_not_a_crash(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Recorded live on 2026-11-13: `uv cache size` on a 99 GiB cache outran a fixed 120 s timeout and the pass crashed."""

    def hangs(argv: list[str], **_kwargs: Any) -> None:
        raise subprocess.TimeoutExpired(argv, 1)

    monkeypatch.setattr(subprocess, "run", hangs)
    with pytest.raises(OSError, match="no answer"):
        ProcessUvCache().size_bytes("uv", "/c/uv-cache", 1)
    assert DockerCiHostState(1).is_idle("c") is None
    assert DockerContainerMounts(1).mount_sources() is None

    def missing(argv: list[str], **_kwargs: Any) -> None:
        raise FileNotFoundError(argv[0])

    monkeypatch.setattr(subprocess, "run", missing)
    with pytest.raises(OSError, match="not installed"):
        ProcessUvCache().prune("uv", "/c/uv-cache", False, 1)
    assert DockerCiHostState(1).is_idle("c") is None
