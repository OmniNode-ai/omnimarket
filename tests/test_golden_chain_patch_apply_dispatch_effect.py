# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-17427: resume patches blocked lanes leave are applied and landed by a dispatched lane."""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import yaml
from omnibase_core.validators.no_unguarded_git_subprocess import scrub_git_location_env

from omnimarket.nodes.node_patch_apply_dispatch_effect.handlers import (
    handler_patch_apply_dispatch as h,
)
from omnimarket.nodes.node_patch_apply_dispatch_effect.models import (
    ModelPatchApplyDispatchRequest,
    ModelPatchApplyDispatchResult,
)

pytestmark = pytest.mark.unit
NOW = datetime(2026, 10, 17, 14, 0, tzinfo=UTC)
LANE = "sample-lane-r2"
PATCH = "sample-omn-17427-change.patch"
GIT_ENV = {
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@example.invalid",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@example.invalid",
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_SYSTEM": os.devnull,
}


def git(cwd: Path, *argv: str) -> str:
    done = subprocess.run(
        ["git", *argv],
        cwd=cwd,
        env={
            **scrub_git_location_env(os.environ),
            **GIT_ENV,
        },
        capture_output=True,
        text=True,
        check=True,
    )
    return done.stdout


class Recorder:
    """Stands in for the remote-lane runner and the ledger appender."""

    def __init__(self, ok: bool = True) -> None:
        self.ok = ok
        self.dispatched: list[dict[str, str]] = []
        self.rows: list[str] = []

    def dispatch(
        self, brief: Path, lane: str, ticket: str, repo: str
    ) -> tuple[bool, str]:
        self.dispatched.append(
            {"brief": brief.read_text(), "lane": lane, "ticket": ticket, "repo": repo}
        )
        return (True, "") if self.ok else (False, "runner unavailable")

    def append(self, row: str) -> tuple[bool, str]:
        self.rows.append(row)
        return True, "rc=0"


class World:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.upstream = root / "upstream"
        self.upstream.mkdir()
        git(self.upstream, "init", "-q", "-b", "main")
        (self.upstream / "a.txt").write_text("one\n")
        git(self.upstream, "add", "a.txt")
        git(self.upstream, "commit", "-q", "-m", "base")
        self.home = root / "registry"
        self.home.mkdir()
        git(root, "clone", "-q", str(self.upstream), str(self.home / "sample"))
        self.patch_dir = self.home / ".onex_state/hook-preflight" / LANE
        self.patch_dir.mkdir(parents=True)
        self.reports = self.home / ".claude_scratch/reports"
        self.reports.mkdir(parents=True)
        (self.reports / f"{LANE}-2026-10-17T0955Z.md").write_text("blocked on a hook\n")
        self.ledger = root / "ROLLING_WORK_LEDGER.md"
        self.ledger.write_text(
            "2026-10-17T09:25:38Z | CLAIM | lane=sample-lane-r2 | ticket=OMN-17427 | x\n"
            "2026-10-17T09:49:31Z | TERMINAL | lane=sample-lane-r2 | ticket=OMN-17427 | "
            "closes-CLAIM=2026-10-17T09:25:38Z | outcome=blocked | patch left\n"
        )
        self.patch = self.patch_dir / PATCH
        scratch = root / "scratch"
        git(root, "clone", "-q", str(self.upstream), str(scratch))
        (scratch / "a.txt").write_text("one\ntwo\n")
        self.patch.write_text(git(scratch, "diff"))
        self.scratch = scratch
        self.age(hours=1)

    def age(self, hours: float) -> None:
        stamp = (NOW - timedelta(hours=hours)).timestamp()
        os.utime(self.patch, (stamp, stamp))

    def land_upstream(self) -> None:
        (self.upstream / "a.txt").write_text("one\ntwo\n")
        git(self.upstream, "commit", "-q", "-am", "same change landed")
        git(self.home / "sample", "fetch", "-q", "origin")

    def request(self, **kwargs: object) -> ModelPatchApplyDispatchRequest:
        return ModelPatchApplyDispatchRequest.model_validate(
            {
                "now": NOW,
                "registry_root": self.home,
                "ledger_path": self.ledger,
                "state_dir": self.root / "state",
                "repos": ("sample", "other"),
                **kwargs,
            }
        )


@pytest.fixture
def world(tmp_path: Path) -> World:
    return World(tmp_path)


def tick(
    world: World, recorder: Recorder, **kwargs: object
) -> ModelPatchApplyDispatchResult:
    return h.HandlerPatchApplyDispatch(
        dispatch=recorder.dispatch, append_row=recorder.append
    ).handle(world.request(**kwargs))


def test_unapplied_patch_dispatches_one_lane_with_patch_and_report_in_the_brief(
    world: World,
) -> None:
    recorder = Recorder()
    result = tick(world, recorder)
    (finding,) = result.findings
    assert finding.disposition == "dispatched"
    (call,) = recorder.dispatched
    assert call["repo"] == "sample"
    assert call["ticket"] == "OMN-17427"
    assert call["lane"] == finding.apply_lane
    assert call["lane"].startswith("mech-patch-apply-sample-lane-r2-")
    assert str(world.patch) in call["brief"]
    assert f"{LANE}-2026-10-17T0955Z.md" in call["brief"]
    assert "blocked on a hook" in call["brief"]
    assert "needs=land" in call["brief"]
    assert world.patch.exists()
    assert result.exit_code == 0
    (row,) = recorder.rows
    assert " | STATUS | lane=mech-patch-auto-review | " in row
    assert f"apply_lane={finding.apply_lane}" in row
    assert "dispatched=yes" in row


def test_patch_already_on_the_default_branch_is_closed_out_and_not_dispatched(
    world: World,
) -> None:
    world.land_upstream()
    recorder = Recorder()
    result = tick(world, recorder)
    (finding,) = result.findings
    assert finding.disposition == "closed-applied"
    assert recorder.dispatched == []
    assert not world.patch.exists()
    closed = world.patch_dir / "closed" / PATCH
    assert closed.is_file()
    record = json.loads(
        (world.patch_dir / "closed" / f"{PATCH}.closed.json").read_text()
    )
    assert record["reason"] == "applied"
    assert record["sha256"] == finding.sha256


def test_patch_older_than_48_hours_is_closed_out_and_not_dispatched(
    world: World,
) -> None:
    world.age(hours=49)
    recorder = Recorder()
    (finding,) = tick(world, recorder).findings
    assert finding.disposition == "closed-expired"
    assert recorder.dispatched == []
    assert (world.patch_dir / "closed" / PATCH).is_file()


def test_patch_at_47_hours_is_still_dispatched(world: World) -> None:
    world.age(hours=47)
    recorder = Recorder()
    (finding,) = tick(world, recorder).findings
    assert finding.disposition == "dispatched"


def test_a_live_claim_on_the_lane_family_holds_the_patch(world: World) -> None:
    with world.ledger.open("a") as handle:
        handle.write(
            "2026-10-17T13:30:00Z | CLAIM | lane=sample-lane-r3 | ticket=OMN-17427 | y\n"
        )
    recorder = Recorder()
    (finding,) = tick(world, recorder).findings
    assert finding.disposition == "held-owned"
    assert recorder.dispatched == []
    assert world.patch.exists()


def test_a_claim_older_than_45_minutes_or_already_closed_does_not_hold_it(
    world: World,
) -> None:
    with world.ledger.open("a") as handle:
        handle.write(
            "2026-10-17T13:10:00Z | CLAIM | lane=sample-lane-r3 | ticket=OMN-17427 | y\n"
        )
        handle.write(
            "2026-10-17T13:40:00Z | CLAIM | lane=sample-lane-r4 | ticket=OMN-17427 | y\n"
        )
        handle.write(
            "2026-10-17T13:50:00Z | TERMINAL | lane=sample-lane-r4 | ticket=OMN-17427 | "
            "closes-CLAIM=2026-10-17T13:40:00Z | outcome=done | z\n"
        )
    recorder = Recorder()
    (finding,) = tick(world, recorder).findings
    assert finding.disposition == "dispatched"


def test_the_same_patch_is_not_dispatched_again_inside_the_hold_window(
    world: World,
) -> None:
    recorder = Recorder()
    tick(world, recorder)
    (second,) = tick(world, recorder, now=NOW + timedelta(hours=1)).findings
    assert second.disposition == "held-recent"
    assert len(recorder.dispatched) == 1
    (third,) = tick(world, recorder, now=NOW + timedelta(hours=7)).findings
    assert third.disposition == "dispatched"
    assert len(recorder.dispatched) == 2


def test_a_changed_patch_is_dispatched_again_at_once(world: World) -> None:
    recorder = Recorder()
    tick(world, recorder)
    (world.scratch / "a.txt").write_text("one\ntwo\nthree\n")
    world.patch.write_text(git(world.scratch, "diff"))
    world.age(hours=1)
    (finding,) = tick(world, recorder, now=NOW + timedelta(minutes=10)).findings
    assert finding.disposition == "dispatched"


def test_a_dispatch_the_runner_refuses_is_reported_and_fails_the_tick(
    world: World,
) -> None:
    recorder = Recorder(ok=False)
    result = tick(world, recorder)
    (finding,) = result.findings
    assert finding.disposition == "dispatch-failed"
    assert "runner unavailable" in finding.detail
    assert result.exit_code == 1
    assert "dispatched=no" in recorder.rows[0]
    again = tick(world, recorder, now=NOW + timedelta(minutes=5))
    assert again.findings[0].disposition == "dispatch-failed"


def test_a_patch_naming_no_registry_clone_is_listed_unroutable(world: World) -> None:
    stray = world.patch_dir / "mystery-change.patch"
    stray.write_text(world.patch.read_text())
    recorder = Recorder()
    result = tick(world, recorder)
    by_name = {Path(f.patch).name: f for f in result.findings}
    assert by_name["mystery-change.patch"].disposition == "unroutable"
    assert by_name[PATCH].disposition == "dispatched"
    assert result.exit_code == 1


def test_dry_run_reads_and_reports_but_changes_nothing(world: World) -> None:
    world.age(hours=49)
    other = world.patch_dir / "sample-omn-17427-second.patch"
    other.write_text(world.patch.read_text())
    recorder = Recorder()
    result = tick(world, recorder, apply=False)
    assert {f.disposition for f in result.findings} == {"dry-run"}
    assert recorder.dispatched == []
    assert recorder.rows == []
    assert world.patch.exists()
    assert other.exists()


def test_per_tick_cap_leaves_the_rest_for_the_next_tick(world: World) -> None:
    for name in ("sample-omn-17427-b.patch", "sample-omn-17427-c.patch"):
        (world.patch_dir / name).write_text(world.patch.read_text() + f"\n# {name}\n")
    recorder = Recorder()
    result = tick(world, recorder, max_dispatch=2)
    assert sorted(f.disposition for f in result.findings) == [
        "deferred",
        "dispatched",
        "dispatched",
    ]
    assert len(recorder.dispatched) == 2


def test_heartbeat_is_written_on_a_quiet_tick(world: World) -> None:
    world.patch.unlink()
    result = tick(world, Recorder())
    assert result.findings == ()
    beat = json.loads((world.root / "state/last-run.json").read_text())
    assert beat["ran_utc"] == "2026-10-17T14:00:00Z"
    assert beat["patches"] == 0


def test_patches_under_the_closed_directory_are_not_scanned(world: World) -> None:
    world.age(hours=49)
    recorder = Recorder()
    tick(world, recorder)
    again = tick(world, recorder, now=NOW + timedelta(minutes=5))
    assert again.findings == ()


def test_a_mutating_patch_that_conflicts_with_the_base_is_still_dispatched(
    world: World,
) -> None:
    (world.upstream / "a.txt").write_text("one\nelsewhere\n")
    git(world.upstream, "commit", "-q", "-am", "diverge")
    git(world.home / "sample", "fetch", "-q", "origin")
    recorder = Recorder()
    (finding,) = tick(world, recorder).findings
    assert finding.disposition == "dispatched"
    assert "does not apply cleanly" in recorder.dispatched[0]["brief"]


def test_contract_names_the_handler_and_declares_the_bus_topics() -> None:
    import importlib
    import tomllib

    root = Path(h.__file__).resolve().parents[1]
    contract = yaml.safe_load((root / "contract.yaml").read_text())
    handler = contract["handler_routing"]["handlers"][0]
    assert handler["handler"]["name"] == "HandlerPatchApplyDispatch"
    assert handler["handler"]["module"] == h.__name__
    assert contract["event_bus"]["publish_topics"] == [
        "onex.evt.omnimarket.patch-apply-dispatch-completed.v1"
    ]
    assert contract["terminal_event"] in contract["event_bus"]["publish_topics"]
    assert (
        contract["runtime_dispatch"]["command_topic"]
        in contract["event_bus"]["subscribe_topics"]
    )
    assert all(
        ".omnimarket." in t
        for t in contract["event_bus"]["subscribe_topics"]
        + contract["event_bus"]["publish_topics"]
    )
    metadata = yaml.safe_load((root / "metadata.yaml").read_text())
    project = tomllib.loads((root.parents[3] / "pyproject.toml").read_text())
    assert (
        project["project"]["entry-points"]["onex.nodes"][root.name]
        == metadata["entry_points"]["onex.nodes"][root.name]
    )
    node = importlib.import_module(f"omnimarket.nodes.{root.name}.node")
    assert node.NodePatchApplyDispatchEffect


async def test_contract_auto_wires_in_the_effects_runtime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from omnibase_core.container import ModelONEXContainer
    from omnibase_infra.event_bus.event_bus_inmemory import EventBusInmemory
    from omnibase_infra.runtime.auto_wiring.discovery import (
        discover_contracts_from_paths,
    )
    from omnibase_infra.runtime.auto_wiring.handler_wiring import wire_from_manifest
    from omnibase_infra.runtime.auto_wiring.models import ModelAutoWiringManifest
    from omnibase_infra.runtime.message_dispatch_engine import MessageDispatchEngine
    from omnibase_infra.topology import load_topology_profile

    monkeypatch.delenv("ONEX_ACTIVE_RUNTIME_PACKAGES", raising=False)
    contract = Path(h.__file__).resolve().parents[1] / "contract.yaml"
    discovered = discover_contracts_from_paths([contract])
    assert not discovered.errors
    assert len(discovered.contracts) == 1
    bus = EventBusInmemory()
    try:
        report = await wire_from_manifest(
            ModelAutoWiringManifest(contracts=tuple(discovered.contracts)),
            MessageDispatchEngine(),
            event_bus=bus,
            environment="local",
            container=ModelONEXContainer(),
            topology=load_topology_profile("local"),
        )
    finally:
        await bus.close()
    assert report.total_wired == 1
    assert not report.quarantined_handlers


def test_the_tick_runs_through_the_event_bus_and_returns_its_typed_terminal(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    from omnimarket.nodes.node_patch_apply_dispatch_effect.handlers import (
        handler_patch_apply_dispatch_cli as cli,
    )

    world.patch.unlink()
    result = cli.dispatch(world.request())
    assert isinstance(result, ModelPatchApplyDispatchResult)
    assert result.summary.startswith("patch-apply-dispatch: patches=0")
    assert (world.root / "state/last-run.json").is_file()


def test_the_runner_uses_host_configuration_and_a_stated_command_wins(
    tmp_path: Path,
) -> None:
    script = tmp_path / "plugin/skills/remote-lane/scripts/onex_remote_lane.py"
    script.parent.mkdir(parents=True)
    script.write_text("")
    assert h.runner_command({"OMNI_PLUGIN_ROOT": str(tmp_path / "plugin")}) == (
        "python3",
        str(script),
        "run",
    )
    assert h.runner_command(
        {
            h.DISPATCH_COMMAND_ENV: "echo run",
            "OMNI_PLUGIN_ROOT": str(tmp_path / "plugin"),
        }
    ) == ("echo", "run")
    assert h.runner_command({}) == ()


def test_a_runner_that_refuses_the_brief_is_a_failed_dispatch(tmp_path: Path) -> None:
    brief = tmp_path / "b.md"
    brief.write_text("x")
    refuse = h.runner_dispatch(
        (
            "python3",
            "-c",
            "import sys; print('REMOTE-LANE-NO-TICKET'); sys.exit(2)",
            "--",
        ),
        tmp_path / "l",
    )
    ok, detail = refuse(brief, "mech-patch-apply-x", "OMN-1", "sample")
    assert not ok
    assert "exited 2" in detail
    assert "REMOTE-LANE-NO-TICKET" in detail
    accept = h.runner_dispatch(("python3", "-c", "pass", "--"), tmp_path / "l")
    assert accept(brief, "mech-patch-apply-y", "OMN-1", "sample") == (True, "")
    assert (
        h.runner_dispatch((), tmp_path / "l")(brief, "z", "OMN-1", "sample")[0] is False
    )


def test_git_reads_ignore_an_inherited_worktree_environment(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GIT_DIR", str(world.scratch / ".git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(world.scratch))
    monkeypatch.setenv("GIT_INDEX_FILE", str(world.root / "foreign-index"))
    assert h._apply_facts(world.home / "sample", world.patch.read_text()) == (
        "origin/main",
        True,
        False,
    )
    assert not (world.root / "foreign-index").exists()


def test_bus_tick_dispatches_with_configured_host_adapters(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    from omnimarket.nodes.node_patch_apply_dispatch_effect.handlers import (
        handler_patch_apply_dispatch_cli as cli,
    )

    writer = world.root / "writer.py"
    rows = world.root / "appended.json"
    writer.write_text(
        "import json, pathlib, sys\n"
        "pathlib.Path(sys.argv[1]).write_text(json.dumps(sys.argv[2:]))\n"
    )
    monkeypatch.setenv(
        h.APPEND_COMMAND_ENV, shlex.join([sys.executable, str(writer), str(rows)])
    )
    monkeypatch.setenv(
        h.DISPATCH_COMMAND_ENV, shlex.join([sys.executable, "-c", "pass", "--"])
    )
    request = world.request()
    result = cli.dispatch(request)
    assert result.exit_code == 0
    assert result.dispatched == 1
    assert result.correlation_id == request.correlation_id
    written = json.loads(rows.read_text())
    assert written[:2] == [str(world.ledger), "--append"]
    assert "dispatched=yes" in written[2]
    assert world.patch.exists()


def test_ledger_writer_refusal_is_reported_and_fails_the_tick(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(
        h.APPEND_COMMAND_ENV,
        shlex.join([sys.executable, "-c", "import sys; sys.exit(2)"]),
    )
    result = h.HandlerPatchApplyDispatch(dispatch=Recorder().dispatch).handle(
        world.request()
    )
    assert result.exit_code == 1
    assert any("status row not appended: exit 2" in error for error in result.errors)


def test_cli_requires_a_host_registry_and_accepts_a_configured_registry(
    world: World, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from omnimarket.nodes.node_patch_apply_dispatch_effect.handlers import (
        handler_patch_apply_dispatch_cli as cli,
    )

    monkeypatch.delenv("ONEX_REPO_REGISTRY", raising=False)
    argv = [
        "--omni-home",
        str(world.home),
        "--ledger",
        str(world.ledger),
        "--now",
        NOW.isoformat(),
        "--dry-run",
    ]
    with pytest.raises(SystemExit) as exc:
        cli.main(argv)
    assert exc.value.code == 2
    assert "ONEX_REPO_REGISTRY" in capsys.readouterr().err
    registry = world.root / "repos.yaml"
    registry.write_text("repositories:\n  - name: sample\n")
    monkeypatch.setenv("ONEX_REPO_REGISTRY", str(registry))
    assert cli.main(argv) == 0
    assert "would dispatch" in capsys.readouterr().out


def test_unreadable_ledger_defers_a_patch_without_dispatching(world: World) -> None:
    world.ledger.unlink()
    recorder = Recorder()
    result = tick(world, recorder)
    assert result.exit_code == 1
    assert result.findings[0].disposition == "deferred"
    assert recorder.dispatched == []
    assert world.patch.exists()
