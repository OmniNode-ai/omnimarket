# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Conservative Docker and temporary directory hygiene with synthetic facts."""

from __future__ import annotations

import os
import subprocess
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest
import yaml

from omnimarket.nodes.node_lab_disk_hygiene_effect.handlers.handler_lab_disk_hygiene import (
    HandlerLabDiskHygiene,
    declared_lane_objects,
    mapped_paths,
)
from omnimarket.nodes.node_lab_disk_hygiene_effect.models import (
    ModelLabDiskHygieneDeployment,
    model_lab_disk_hygiene_request,
)

pytestmark = pytest.mark.unit

OLD = "2026-10-01T00:00:00.000000000Z"
NEW = "2099-01-01T00:00:00.000000000Z"


def _census(tmp: Path) -> Path:
    path = tmp / "lane-manifest.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "hosts": {"lab-a": {"aliases": ["lab-a"]}},
                "lanes": {
                    "lane-dev": {
                        "hosts": ["lab-a"],
                        "compose_project": "lane-dev",
                        "services": [
                            {"name": "lane-dev-postgres", "kind": "service"},
                            {"name": "lane-dev-forward-migration"},
                        ],
                    },
                    "lane-sim": {"hosts": ["lab-a"], "compose_project": "lane-sim"},
                },
            }
        )
    )
    return path


class FakeDocker:
    """Answers the docker CLI calls the handler makes; records every argv."""

    def __init__(
        self, containers: list[tuple[str, str, str, str, str]], version: str = "29.8.1"
    ):
        # (id, name, state, compose project, finished at)
        self.containers = containers
        self.version = version
        self.calls: list[list[str]] = []
        self.free = 100

    def run(self, argv: Sequence[str], **_: Any) -> subprocess.CompletedProcess[str]:
        argv = list(argv)
        self.calls.append(argv)
        out = ""
        if argv[1:2] == ["version"]:
            out = self.version + "\n"
        elif argv[1:3] == ["ps", "-a"]:
            out = "".join(f"{c[0]}\t{c[1]}\t{c[2]}\t{c[3]}\n" for c in self.containers)
        elif argv[1:2] == ["inspect"]:
            ids = argv[4:]
            out = "".join(f"{c[0]}\t{c[4]}\n" for c in self.containers if c[0] in ids)
        elif argv[1:3] == ["builder", "prune"]:
            self.free += 50
            out = "Total:\t50GB\n"
        elif argv[1] == "rm" or argv[1:3] in (["image", "prune"], ["volume", "prune"]):
            self.free += 1
            out = "Total reclaimed space: 1GB\n"
        return subprocess.CompletedProcess(argv, 0, out, "")

    def disk_free(self, _: Path) -> int:
        return self.free


CONTAINERS = [
    ("a1", "lane-dev-forward-migration", "exited", "lane-dev", OLD),
    ("a2", "ci-runner-6", "exited", "ci-runner", OLD),
    ("a3", "ci-runner-7", "running", "ci-runner", OLD),
    ("a4", "landing-l9-pg", "exited", "", OLD),
    ("a5", "fresh-test-pg", "exited", "", NEW),
    ("a6", "rlane-x-pg", "exited", "rlane-x", OLD),
    ("a7", "lane-sim-postgres", "exited", "", OLD),
]


def _handle(tmp: Path, docker: FakeDocker, **kw: Any) -> Any:
    handler = HandlerLabDiskHygiene(
        overlay=ModelLabDiskHygieneDeployment("unused.yaml"),
        run=docker.run,
        disk_free=docker.disk_free,
        which=lambda _: "d",
    )
    request = model_lab_disk_hygiene_request.ModelLabDiskHygieneRequest(
        omni_home=tmp, census=_census(tmp), **kw
    )
    return handler.handle(request)


def test_census_names_every_declared_project_and_container(tmp_path: Path) -> None:
    projects, names = declared_lane_objects(_census(tmp_path))
    assert projects == {"lane-dev", "lane-sim"}
    assert "lane-dev-forward-migration" in names


def test_exited_containers_of_no_lane_go_and_lane_or_running_projects_stay(
    tmp_path: Path,
) -> None:
    docker = FakeDocker(CONTAINERS)
    result = _handle(tmp_path, docker)
    assert result.docker == "ran"
    assert set(result.removed_containers) == {"landing-l9-pg", "rlane-x-pg"}
    kept = set(result.kept_containers)
    # a declared lane's oneshot, a project with a running container, a fresh exit, and a
    # container named under a declared project even without a compose label
    assert kept == {
        "lane-dev-forward-migration",
        "ci-runner-6",
        "fresh-test-pg",
        "lane-sim-postgres",
    }
    (rm,) = [c for c in docker.calls if c[1] == "rm"]
    assert sorted(rm[2:]) == ["a4", "a6"]
    assert "-f" not in rm


def test_prunes_cap_the_build_cache_and_spare_named_volumes(tmp_path: Path) -> None:
    docker = FakeDocker(CONTAINERS)
    result = _handle(tmp_path, docker, build_cache_max_gb=10, image_unused_hours=48)
    calls = [" ".join(c[1:]) for c in docker.calls]
    assert "builder prune -af --max-used-space 10gb" in calls
    assert "image prune -af --filter until=48h" in calls
    assert "volume prune -f" in calls  # anonymous only on Docker 23 and later
    assert not any("volume prune -a" in c or "system prune" in c for c in calls)
    assert result.freed_bytes == 53
    assert "docker_freed=" in result.line()


def test_an_old_engine_keeps_named_volumes_and_uses_keep_storage(
    tmp_path: Path,
) -> None:
    docker = FakeDocker([], version="20.10.24")
    real = docker.run

    def old(argv: Sequence[str], **kw: Any) -> subprocess.CompletedProcess[str]:
        if "--max-used-space" in argv:
            docker.calls.append(list(argv))
            return subprocess.CompletedProcess(
                list(argv), 125, "", "unknown flag: --max-used-space"
            )
        return real(argv, **kw)

    handler = HandlerLabDiskHygiene(
        overlay=ModelLabDiskHygieneDeployment("unused.yaml"),
        run=old,
        disk_free=docker.disk_free,
        which=lambda _: "d",
    )
    handler.handle(
        model_lab_disk_hygiene_request.ModelLabDiskHygieneRequest(
            omni_home=tmp_path, census=_census(tmp_path)
        )
    )
    calls = [" ".join(c[1:]) for c in docker.calls]
    assert "builder prune -af --keep-storage 10gb" in calls
    assert not any(c.startswith("volume prune") for c in calls)


def test_an_unreadable_census_removes_no_container(tmp_path: Path) -> None:
    docker = FakeDocker(CONTAINERS)
    handler = HandlerLabDiskHygiene(
        overlay=ModelLabDiskHygieneDeployment("unused.yaml"),
        run=docker.run,
        disk_free=docker.disk_free,
        which=lambda _: "d",
    )
    result = handler.handle(
        model_lab_disk_hygiene_request.ModelLabDiskHygieneRequest(
            omni_home=tmp_path, census=tmp_path / "missing.yaml"
        )
    )
    assert result.removed_containers == ()
    assert not any(c[1] == "rm" for c in docker.calls)
    assert any("census" in e for e in result.errors)
    assert any(c[1:3] == ["builder", "prune"] for c in docker.calls)


def test_dry_run_lists_and_prunes_nothing(tmp_path: Path) -> None:
    docker = FakeDocker(CONTAINERS)
    result = _handle(tmp_path, docker, dry_run=True)
    assert result.docker == "dry-run"
    assert set(result.removed_containers) == {"landing-l9-pg", "rlane-x-pg"}
    assert not any(c[1] in ("rm", "builder", "image", "volume") for c in docker.calls)


def test_no_docker_is_reported_not_failed(tmp_path: Path) -> None:
    handler = HandlerLabDiskHygiene(
        overlay=ModelLabDiskHygieneDeployment("unused.yaml"),
        run=None,
        disk_free=lambda _: 1,
        which=lambda _: None,
    )
    result = handler.handle(
        model_lab_disk_hygiene_request.ModelLabDiskHygieneRequest(omni_home=tmp_path)
    )
    assert result.docker == "absent"
    assert result.errors == ()


# Stale temporary directories.


def _aged(path: Path, hours: float) -> None:
    when = time.time() - hours * 3600
    for p in [path, *path.rglob("*")] if path.is_dir() else [path]:
        os.utime(p, (when, when), follow_symlinks=False)


def test_stale_tmp_entries_go_and_fresh_held_or_foreign_ones_stay(
    tmp_path: Path,
) -> None:
    tmp = tmp_path / "tmp"
    (tmp / "l1517" / "uv-cache").mkdir(parents=True)
    (tmp / "l1517" / "uv-cache" / "wheel.whl").write_bytes(b"0" * 4096)
    (tmp / "old-file.log").write_text("x" * 100)
    (tmp / "l2000" / "deep").mkdir(parents=True)
    (tmp / "l2000" / "deep" / "new.txt").write_text("fresh")
    (tmp / "held-dir").mkdir()
    (tmp / "tmux-1000").mkdir()
    for name in ("l1517", "old-file.log", "held-dir", "tmux-1000"):
        _aged(tmp / name, 48)
    _aged(tmp / "l2000", 48)
    os.utime(tmp / "l2000" / "deep" / "new.txt")  # one fresh file keeps the whole entry
    handler = HandlerLabDiskHygiene(
        overlay=ModelLabDiskHygieneDeployment("unused.yaml"),
        run=None,
        disk_free=lambda _: 0,
        which=lambda _: None,
        held_paths=lambda: {str(tmp / "held-dir" / "x.sock")},
    )
    result = handler.handle(
        model_lab_disk_hygiene_request.ModelLabDiskHygieneRequest(
            omni_home=tmp_path, tmp_dirs=(tmp,), tmp_stale_hours=24
        )
    )
    assert sorted(result.removed_tmp) == ["l1517", "old-file.log"]
    assert not (tmp / "l1517").exists()
    assert not (tmp / "old-file.log").exists()
    assert (tmp / "l2000").exists()
    assert (tmp / "held-dir").exists()
    assert (tmp / "tmux-1000").exists()
    assert result.removed_tmp_bytes >= 4096 + 100
    assert "tmp_removed=2" in result.line()


def test_stale_tmp_is_skipped_when_process_paths_are_unknown(tmp_path: Path) -> None:
    tmp = tmp_path / "tmp"
    (tmp / "old").mkdir(parents=True)
    _aged(tmp / "old", 48)
    handler = HandlerLabDiskHygiene(
        overlay=ModelLabDiskHygieneDeployment("unused.yaml"),
        run=None,
        disk_free=lambda _: 0,
        which=lambda _: None,
        held_paths=lambda: None,
    )
    result = handler.handle(
        model_lab_disk_hygiene_request.ModelLabDiskHygieneRequest(
            omni_home=tmp_path, tmp_dirs=(tmp,)
        )
    )
    assert (tmp / "old").exists()
    assert result.removed_tmp == ()
    assert any("process" in e for e in result.errors)


def test_stale_tmp_dry_run_lists_and_removes_nothing(tmp_path: Path) -> None:
    tmp = tmp_path / "tmp"
    (tmp / "old").mkdir(parents=True)
    _aged(tmp / "old", 48)
    handler = HandlerLabDiskHygiene(
        overlay=ModelLabDiskHygieneDeployment("unused.yaml"),
        run=None,
        disk_free=lambda _: 0,
        which=lambda _: None,
        held_paths=lambda: set(),
    )
    result = handler.handle(
        model_lab_disk_hygiene_request.ModelLabDiskHygieneRequest(
            omni_home=tmp_path, tmp_dirs=(tmp,), dry_run=True
        )
    )
    assert result.removed_tmp == ("old",)
    assert (tmp / "old").exists()


def test_maps_lines_without_a_path_are_skipped_not_indexed() -> None:
    # Anonymous mappings have five fields followed by padding.
    maps = (
        "7f0000000000-7f0000001000 r-xp 00000000 103:02 1234      /tmp/l1517/lib.so\n"
        "7f0000002000-7f0000003000 rw-p 00000000 00:00 0          \n"
        "7ffd00000000-7ffd00021000 rw-p 00000000 00:00 0          [stack]\n"
    )
    assert mapped_paths(maps) == {"/tmp/l1517/lib.so"}
