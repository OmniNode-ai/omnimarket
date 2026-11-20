"""Tests for the capability-catalog timer and publish.

Every fixture repository is local: a bare "origin" plus a clone standing in for
docs_repo,
GitHub is a recorded fake, and git runs with the scrubbed environment.
"""

from __future__ import annotations

import json
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from omnimarket.nodes.node_capability_catalog_effect.handlers import (
    handler_capability_catalog_publish as pub,
)
from omnimarket.nodes.node_capability_catalog_effect.models.model_capability_catalog import (
    SCHEMA_ID,
    Catalog,
    Host,
    Lane,
    LaneStatus,
    Probe,
    ProbeState,
)

pytestmark = pytest.mark.unit


def _git(cwd: Path, *args: str) -> str:
    done = subprocess.run(
        ["git", "-C", str(cwd), *args],
        capture_output=True,
        text=True,
        check=False,
        env=pub.scrubbed_git_env(),
    )
    assert done.returncode == 0, f"git {' '.join(args)} failed: {done.stderr}"
    return done.stdout


class FakeGh:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def run(self, args: list[str]) -> subprocess.CompletedProcess[str]:
        self.calls.append(args)
        head = "HTTP/2.0 200 OK\n\n"
        if args[:2] == ["api", "-i"] and "-X" not in args:
            # The lane gh shim refuses every read; a read here is a regression.
            return subprocess.CompletedProcess(
                args, 1, "", "gh: reads are refused from a lane"
            )
        body = json.dumps({"number": 42}) if "POST" in args else "{}"
        return subprocess.CompletedProcess(args, 0, head + body, "")

    def github(self) -> pub.GitHub:
        return pub.GitHub(run=self.run, repo="OmniNode-ai/docs_repo")


def _host(host_id: str = "lab-200") -> Host:
    probe = Probe(
        state=ProbeState.OK,
        probe="docker ps -a",
        exit_code=0,
        read_at="2026-09-28T00:00:00Z",
    )
    return Host(host_id=host_id, display_name=".200", state=ProbeState.OK, probe=probe)


def _lane(status: LaneStatus = LaneStatus.FREE, host_id: str = "lab-200") -> Lane:
    probe = Probe(
        state=ProbeState.OK,
        probe="docker ps -a",
        exit_code=0,
        read_at="2026-09-28T00:00:00Z",
    )
    return Lane(
        lane_id="dev-200",
        host_id=host_id,
        compose_project="omnibase-infra-dev-200",
        status=status,
        probe=probe,
        note="28 containers, 23 up",
    )


def make_collect(
    status: LaneStatus = LaneStatus.FREE, host_id: str = "lab-200"
) -> pub.CollectFn:
    def collect(
        sources: Any, *, registry_root: Path, now: str
    ) -> tuple[list[Host], list[Lane], list[Any]]:
        return [_host(host_id)], [_lane(status, host_id)], []

    return collect


def failing_collect(
    sources: Any, *, registry_root: Path, now: str
) -> tuple[list[Host], list[Lane], list[Any]]:
    raise RuntimeError("ssh unreachable: simulated collector failure")


@pytest.fixture
def kb_repo(tmp_path: Path) -> Path:
    """A bare origin plus a clone standing in for docs-repo."""
    origin = tmp_path / "origin.git"
    _git(tmp_path, "init", "--quiet", "--bare", "-b", "main", str(origin))
    tree = tmp_path / "docs_repo"
    _git(tmp_path, "init", "--quiet", "-b", "main", str(tree))
    _git(tree, "config", "user.name", "fixture")
    _git(tree, "config", "user.email", "person@example.com")
    _git(tree, "config", "commit.gpgsign", "false")
    (tree / "reference").mkdir(parents=True)
    (tree / "reference" / "capability-catalog.md").write_text(
        "# seed\n", encoding="utf-8"
    )
    _git(tree, "add", "--", "reference/capability-catalog.md")
    _git(tree, "commit", "--quiet", "-m", "seed")
    _git(tree, "remote", "add", "origin", str(origin))
    _git(tree, "push", "--quiet", "origin", "main")
    return tree


@pytest.fixture
def sources_path(tmp_path: Path) -> Path:
    path = tmp_path / "sources.yaml"
    path.write_text(
        "schema: omni.capability-catalog.sources/1\n"
        "orchestrator_host: lab-200\n"
        "lane_manifest_path: omnibase_infra/deploy/lane-census/lane-manifest.yaml\n"
        "census_planner_path: omnibase_infra/scripts/lane_census_plan.py\n"
        "ssh_timeout_s: 8\n"
        "curl_timeout_s: 5\n"
        "hosts:\n"
        "  lab-200:\n"
        "    display_name: '.200'\n"
        "    local: true\n"
        "    lanes:\n"
        "      - lane_id: dev-200\n"
        "        compose_project: omnibase-infra-dev-200\n"
        "    model_servers: []\n",
        encoding="utf-8",
    )
    return path


def _stamp(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def watcher_state(
    tmp_path: Path,
    *,
    open_number: int | None = None,
    branch: str = pub.BRANCH,
    state: str = "OPEN",
    age: timedelta = timedelta(seconds=30),
) -> Path:
    """A PR watcher state file as the watcher writes it: last_tick and facts per PR record."""
    prs: dict[str, object] = {}
    if open_number is not None:
        prs[f"docs_repo#{open_number}"] = {
            "facts": {
                "repo": "docs_repo",
                "number": open_number,
                "head_ref": branch,
                "state": state,
            }
        }
    path = tmp_path / "pr-watcher-state.json"
    path.write_text(
        json.dumps(
            {"schema": 1, "last_tick": _stamp(datetime.now(UTC) - age), "prs": prs}
        ),
        encoding="utf-8",
    )
    return path


def _run(
    kb_repo: Path,
    sources_path: Path,
    tmp_path: Path,
    gh: FakeGh,
    *,
    collect: pub.CollectFn | None = None,
    clock: pub.Clock = lambda: "2026-09-28T13:00:00Z",
    pr_state: Path | None = None,
) -> int:
    return pub.run(
        registry_root=tmp_path,
        onex_state_dir=tmp_path / "state",
        kb_repo=kb_repo,
        sources_path=sources_path,
        worktree=tmp_path / "wt" / "docs_repo",
        collect=collect or make_collect(),
        clock=clock,
        github=gh.github(),
        pr_state=pr_state or watcher_state(tmp_path),
        log=lambda _msg: None,
    )


# --------------------------------------------------------------------------
# AC1: an unchanged structural hash makes no GitHub call


def test_no_change_no_call(kb_repo: Path, sources_path: Path, tmp_path: Path) -> None:
    gh = FakeGh()
    rc1 = _run(kb_repo, sources_path, tmp_path, gh)
    assert rc1 == 0
    first_calls = len(gh.calls)
    assert first_calls > 0  # the first run, hash changed from "none", does publish

    rc2 = _run(kb_repo, sources_path, tmp_path, gh)
    assert rc2 == 0
    assert len(gh.calls) == first_calls, (
        "a second run with an unchanged catalog made a GitHub call"
    )


def test_changed_hash_does_publish_and_opens_a_pr(
    kb_repo: Path, sources_path: Path, tmp_path: Path
) -> None:
    gh = FakeGh()
    assert (
        _run(kb_repo, sources_path, tmp_path, gh, collect=make_collect(LaneStatus.FREE))
        == 0
    )
    calls_after_first = len(gh.calls)
    assert calls_after_first > 0

    assert (
        _run(kb_repo, sources_path, tmp_path, gh, collect=make_collect(LaneStatus.BUSY))
        == 0
    )
    assert len(gh.calls) > calls_after_first, (
        "a structurally changed catalog must publish again"
    )


# --------------------------------------------------------------------------
# AC2: a collector failure keeps the last good files and writes failure.json


def test_failure_keeps_last_good(
    kb_repo: Path, sources_path: Path, tmp_path: Path
) -> None:
    gh = FakeGh()
    assert _run(kb_repo, sources_path, tmp_path, gh) == 0
    catalog_path = tmp_path / "state" / "capability" / "catalog.json"
    good_catalog = catalog_path.read_text(encoding="utf-8")
    assert (tmp_path / "state" / "capability" / "failure.json").exists() is False

    rc = _run(kb_repo, sources_path, tmp_path, gh, collect=failing_collect)
    assert rc == 1
    assert catalog_path.read_text(encoding="utf-8") == good_catalog, (
        "a failed run must not touch the last good catalog"
    )
    failure = json.loads(
        (tmp_path / "state" / "capability" / "failure.json").read_text(encoding="utf-8")
    )
    assert "ssh unreachable" in failure["reason"]


def test_a_good_run_clears_a_stale_failure(
    kb_repo: Path, sources_path: Path, tmp_path: Path
) -> None:
    gh = FakeGh()
    assert _run(kb_repo, sources_path, tmp_path, gh, collect=failing_collect) == 1
    failure_path = tmp_path / "state" / "capability" / "failure.json"
    assert failure_path.exists()

    assert _run(kb_repo, sources_path, tmp_path, gh) == 0
    assert not failure_path.exists()


# --------------------------------------------------------------------------
# The open-PR read comes from the PR watcher state, never from gh


def _writes(gh: FakeGh) -> list[str]:
    return [" ".join(call) for call in gh.calls]


def test_no_open_pr_in_the_watcher_state_opens_one_without_a_gh_read(
    kb_repo: Path, sources_path: Path, tmp_path: Path
) -> None:
    gh = FakeGh()
    assert _run(kb_repo, sources_path, tmp_path, gh) == 0
    calls = _writes(gh)
    assert len(calls) == 1, calls
    assert "-X POST" in calls[0], calls
    assert "/pulls" in calls[0], calls


def test_an_open_pr_in_the_watcher_state_is_refreshed_not_duplicated(
    kb_repo: Path, sources_path: Path, tmp_path: Path
) -> None:
    gh = FakeGh()
    state = watcher_state(tmp_path, open_number=845)
    assert _run(kb_repo, sources_path, tmp_path, gh, pr_state=state) == 0
    calls = _writes(gh)
    assert len(calls) == 1, calls
    assert "-X PATCH" in calls[0], calls
    assert "pulls/845" in calls[0], calls


@pytest.mark.parametrize(
    ("branch", "pr_state"),
    [("bot/other", "OPEN"), (pub.BRANCH, "MERGED"), (pub.BRANCH, "CLOSED")],
    ids=["other-branch", "merged", "closed"],
)
def test_only_an_open_pr_of_the_catalog_branch_counts(
    kb_repo: Path, sources_path: Path, tmp_path: Path, branch: str, pr_state: str
) -> None:
    gh = FakeGh()
    state = watcher_state(tmp_path, open_number=845, branch=branch, state=pr_state)
    assert _run(kb_repo, sources_path, tmp_path, gh, pr_state=state) == 0
    assert "-X POST" in _writes(gh)[0]


def test_a_stale_watcher_state_stops_the_publish_without_reading_gh(
    kb_repo: Path, sources_path: Path, tmp_path: Path
) -> None:
    gh = FakeGh()
    state = watcher_state(tmp_path, age=timedelta(minutes=30))
    with pytest.raises(pub.PublishError, match="stale"):
        _run(kb_repo, sources_path, tmp_path, gh, pr_state=state)
    assert gh.calls == []
    assert not (tmp_path / "state" / "capability" / "last_published_hash.txt").exists()


def test_a_missing_watcher_state_stops_the_publish_without_reading_gh(
    kb_repo: Path, sources_path: Path, tmp_path: Path
) -> None:
    gh = FakeGh()
    with pytest.raises(pub.PublishError, match="unreadable"):
        _run(kb_repo, sources_path, tmp_path, gh, pr_state=tmp_path / "absent.json")
    assert gh.calls == []


# --------------------------------------------------------------------------
# Atomic writes: every render happens via a temp file + os.replace


def test_render_is_atomic_no_tmp_file_left_behind(
    kb_repo: Path, sources_path: Path, tmp_path: Path
) -> None:
    gh = FakeGh()
    assert _run(kb_repo, sources_path, tmp_path, gh) == 0
    cap_dir = tmp_path / "state" / "capability"
    leftovers = list(cap_dir.glob("*.tmp"))
    assert leftovers == []
    for name in (
        "catalog.json",
        "digest.txt",
        "brief-part.md",
        "capability-catalog.md",
    ):
        assert (cap_dir / name).is_file()


def test_rendered_file_carries_the_ca5_header(
    kb_repo: Path, sources_path: Path, tmp_path: Path
) -> None:
    gh = FakeGh()
    assert _run(kb_repo, sources_path, tmp_path, gh) == 0
    rendered = (tmp_path / "state" / "capability" / "capability-catalog.md").read_text(
        encoding="utf-8"
    )
    assert pub.HEADER_MARKER in rendered
    assert "body-sha256:" in rendered


def test_structural_hash_ignores_timestamps_but_not_lane_status() -> None:
    a = Catalog(
        schema=SCHEMA_ID,
        generated_at="2026-09-28T00:00:00Z",
        hosts=(_host(),),
        lanes=(_lane(LaneStatus.FREE),),
    )
    b = Catalog(
        schema=SCHEMA_ID,
        generated_at="2026-09-28T00:15:00Z",
        hosts=(_host(),),
        lanes=(_lane(LaneStatus.FREE),),
    )
    c = Catalog(
        schema=SCHEMA_ID,
        generated_at="2026-09-28T00:15:00Z",
        hosts=(_host(),),
        lanes=(_lane(LaneStatus.BUSY),),
    )

    assert pub.structural_hash(a) == pub.structural_hash(b)
    assert pub.structural_hash(a) != pub.structural_hash(c)


def _lane_named(
    lane_id: str, host_id: str, *, note: str, read_at: str, exit_code: int
) -> Lane:
    probe = Probe(
        state=ProbeState.OK,
        probe=f"ssh {host_id} docker ps -a",
        exit_code=exit_code,
        read_at=read_at,
    )
    return Lane(
        lane_id=lane_id,
        host_id=host_id,
        compose_project=f"omnibase-infra-{lane_id}",
        status=LaneStatus.FREE,
        probe=probe,
        note=note,
    )


def test_structural_hash_is_order_and_reading_independent() -> None:
    """Two runs over the same hosts and lanes give one hash, whatever the order the
    collector returned them in and whatever the live counts in each lane's note said."""
    first = Catalog(
        schema=SCHEMA_ID,
        generated_at="2026-10-07T15:00:00Z",
        hosts=(_host("lab-200"), _host("lab-201")),
        lanes=(
            _lane_named(
                "dev-200",
                "lab-200",
                note="28 containers, 23 up",
                read_at="a",
                exit_code=0,
            ),
            _lane_named(
                "dev", "lab-201", note="33 containers, 30 up", read_at="a", exit_code=0
            ),
            _lane_named(
                "judge", "lab-201", note="6 containers, 6 up", read_at="a", exit_code=0
            ),
        ),
    )
    second = Catalog(
        schema=SCHEMA_ID,
        generated_at="2026-10-07T15:15:00Z",
        hosts=(_host("lab-201"), _host("lab-200")),
        lanes=(
            _lane_named(
                "judge", "lab-201", note="5 containers, 5 up", read_at="b", exit_code=0
            ),
            _lane_named(
                "dev-200",
                "lab-200",
                note="27 containers, 22 up",
                read_at="b",
                exit_code=0,
            ),
            _lane_named(
                "dev", "lab-201", note="34 containers, 31 up", read_at="b", exit_code=0
            ),
        ),
    )

    assert pub.structural_hash(first) == pub.structural_hash(second)
    assert pub.structural_hash(first) == pub.structural_hash(first)


def test_structural_hash_still_moves_on_a_structural_change() -> None:
    base = Catalog(
        schema=SCHEMA_ID,
        generated_at="2026-10-07T15:00:00Z",
        hosts=(_host("lab-200"),),
        lanes=(_lane_named("dev-200", "lab-200", note="n", read_at="a", exit_code=0),),
    )
    gained_host = Catalog(
        schema=SCHEMA_ID,
        generated_at="2026-10-07T15:00:00Z",
        hosts=(_host("lab-200"), _host("lab-201")),
        lanes=base.lanes,
    )
    gained_lane = Catalog(
        schema=SCHEMA_ID,
        generated_at="2026-10-07T15:00:00Z",
        hosts=base.hosts,
        lanes=(
            *base.lanes,
            _lane_named("dogfood", "lab-200", note="n", read_at="a", exit_code=0),
        ),
    )

    assert pub.structural_hash(base) != pub.structural_hash(gained_host)
    assert pub.structural_hash(base) != pub.structural_hash(gained_lane)
