"""Mutation controls for the lease-based reaper.

Every subject fixture first checks both positive controls on separate scratch stores: a closed,
leased stack is removed with execute, and dry run issues no down. Unsafe cases below are the
negative controls. All ledger append commands and all docker commands terminate in the fake runner.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from omnimarket.handlers.claim_index import ClaimStoreIncompleteError
from omnimarket.nodes.node_lab_stack_reaper_effect.handlers import (
    handler_lab_stack_reaper as reaper,
)
from omnimarket.nodes.node_lab_stack_reaper_effect.handlers import (
    handler_lab_stack_reaper_cli as cli,
)
from omnimarket.nodes.node_lab_stack_reaper_effect.handlers import (
    handler_lab_stack_reaper_leases as leases,
)
from omnimarket.nodes.node_lab_stack_reaper_effect.handlers import (
    lab_stack_support as support,
)
from omnimarket.nodes.node_lab_stack_reaper_effect.handlers.handler_lab_stack_reaper import (
    HandlerLabStackReaper,
)
from omnimarket.nodes.node_lab_stack_reaper_effect.handlers.lab_stack_support import (
    Completed,
)
from omnimarket.nodes.node_lab_stack_reaper_effect.models import (
    ModelLabStackReaperRequest,
    ModelLabStackReaperResult,
)
from omnimarket.nodes.node_lab_stack_reaper_effect.models import (
    model_lab_stack_reaper_request as request_models,
)

LeaseStore = leases.LeaseStore
main = cli.main

pytestmark = pytest.mark.unit
NOW = datetime(2026, 10, 15, 12, tzinfo=UTC)
A = "2026-10-15T10:00:00Z"
B = "2026-10-15T10:01:00Z"
PROJECT = "lab-omn1001-test"
# Synthetic ticket-shaped tokens, so the owner-matching code reads a real shape.
TICKET_A = "OMN-" + "1001"
TICKET_B = "OMN-" + "1002"


def row(kind: str, claim: str = A, lane: str = "worker", closes: str = "") -> str:
    return (
        f"{claim} | {kind} | lane={lane} | ticket={TICKET_A}"
        + (f" | closes-CLAIM={closes}" if closes else "")
        + " | scratch evidence"
    )


class Env:
    def __init__(self, root: Path) -> None:
        root.mkdir()
        self.ledger = root / "ledger.md"
        self.census = root / "census.yaml"
        self.census.write_text(
            "lanes:\n  dev:\n    compose_project: protected-dev\n"
            "    services:\n      - name: declared-runtime\n",
            encoding="utf-8",
        )
        self.store = LeaseStore(root / "leases")
        self.store.directory.mkdir()
        self.projects: dict[str, list[str]] = {PROJECT: ["test-runtime"]}
        self.config: dict[str, str] = {}
        self.networks: dict[str, dict[str, object]] = {}
        self.config_rc = 0
        self.calls: list[list[str]] = []
        self.rows: list[str] = []
        self.failures = 0
        self.abort = False
        self.bad_listing: str | None = None
        self.list_rc = 0
        self.ps_rc = 0
        self.append_rc = 0
        self.write_rows([row("CLAIM"), row("TERMINAL", B, closes=A)])
        self.store.add(PROJECT, "worker", A, "one", ())

    def write_rows(self, rows: Sequence[str]) -> None:
        self.ledger.write_text("\n".join(rows) + "\n", encoding="utf-8")
        os.utime(self.ledger, (NOW.timestamp(), NOW.timestamp()))

    def run(self, argv: Sequence[str]) -> Completed:
        argv = list(argv)
        self.calls.append(argv)
        if "--append" in argv:
            self.rows.append(argv[argv.index("--append") + 1])
            return Completed(
                self.append_rc, "", "append failed" if self.append_rc else ""
            )
        if argv[:2] == ["docker", "-H"]:
            argv = ["docker", *argv[3:]]
        if argv[:3] == ["docker", "compose", "ls"]:
            listing = json.dumps(
                [
                    {"Name": name, "ConfigFiles": self.config.get(name, "")}
                    for name in self.projects
                ]
            )
            return Completed(
                self.list_rc,
                self.bad_listing if self.bad_listing is not None else listing,
                "list failed",
            )
        if argv[:2] == ["docker", "ps"]:
            project = argv[argv.index("--filter") + 1].rsplit("=", 1)[1]
            return Completed(self.ps_rc, "\n".join(self.projects[project]), "ps failed")
        if argv[:3] == ["docker", "compose", "-p"] and "config" in argv:
            return Completed(
                self.config_rc,
                json.dumps({"networks": self.networks.get(argv[3], {})}),
                "config failed",
            )
        if argv[:3] == ["docker", "compose", "-p"] and argv[-1] == "down":
            project = argv[3]
            assert self.store.path(project, "fence").exists(), "down must start fenced"
            assert self.store.path(project, "json").exists(), "retain leases until gone"
            if self.abort:
                raise KeyboardInterrupt
            if self.failures:
                self.failures -= 1
                return Completed(1, "", "transient failure")
            self.projects.pop(project)
            return Completed(0, "", "")
        raise AssertionError(f"unexpected subprocess: {argv}")

    def request(self, **overrides: Any) -> ModelLabStackReaperRequest:
        return replace(
            ModelLabStackReaperRequest(
                "reap",
                lease_dir=self.store.directory,
                ledger=self.ledger,
                census=self.census,
                now=NOW,
                execute=True,
                fence_wait_s=0,
            ),
            **overrides,
        )

    def reap(self, **overrides: Any) -> ModelLabStackReaperResult:
        return HandlerLabStackReaper(self.run).handle(self.request(**overrides))

    def lease(self, **overrides: Any) -> ModelLabStackReaperResult:
        return HandlerLabStackReaper(self.run).handle(
            self.request(
                verb="lease",
                project=PROJECT,
                lane="reuser",
                claim=B,
                generation="one",
                **overrides,
            )
        )

    @property
    def downs(self) -> list[list[str]]:
        return [call for call in self.calls if call[-1] == "down"]


def assert_kept(
    env: Env, result: ModelLabStackReaperResult, decision: str = "kept-live"
) -> None:
    assert any(
        p.project == PROJECT and p.decision == decision for p in result.projects
    ), result
    assert env.downs == []
    assert PROJECT in env.projects
    assert env.store.path(PROJECT, "json").exists()


@pytest.fixture
def env(tmp_path: Path) -> Env:
    # Positive controls run for every mutation, isolated from its negative-control store.
    dry = Env(tmp_path / "dry-control")
    assert dry.reap(execute=False).projects[0].decision == "would-remove"
    assert dry.downs == []
    execute = Env(tmp_path / "execute-control")
    result = execute.reap()
    assert result.ok, result
    assert result.projects[0].decision == "removed", result
    assert len(execute.downs) == 1
    assert PROJECT not in execute.projects
    assert not execute.store.path(PROJECT, "json").exists()
    assert not execute.store.path(PROJECT, "fence").exists()
    return Env(tmp_path / "subject")


def test_reuse_after_initial_read_is_rejudged_under_lock(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = LeaseStore.lock
    injected = False

    @contextmanager
    def lock(store: LeaseStore, project: str) -> Iterator[None]:
        nonlocal injected
        if not injected:
            injected = True
            # The initial snapshot is already closed. A reuser adds a live lease before lock.
            env.store.add(project, "reuser", B, "one", ())
        with original(store, project):
            yield

    monkeypatch.setattr(LeaseStore, "lock", lock)
    result = env.reap()
    assert_kept(env, result)
    assert result.projects[0].open_lanes == ("reuser",)


def test_reuse_during_orphaned_removal_is_refused_by_fence(env: Env) -> None:
    env.store.path(PROJECT, "fence").touch()
    before = env.store.path(PROJECT, "json").read_bytes()
    result = env.lease()
    assert result.decision == "refused"
    assert "fenced" in result.errors[0]
    assert env.store.path(PROJECT, "json").read_bytes() == before


def test_unleased_stack_is_alarmed_and_never_removed(env: Env) -> None:
    env.store.path(PROJECT, "json").unlink()
    result = env.reap()
    assert result.projects[0].decision == "unleased"
    assert result.alarms
    assert env.downs == []
    assert PROJECT in env.projects
    assert sum(" | FRICTION | " in r for r in env.rows) == 1


def test_registered_reuser_keeps_the_creators_closed_stack(env: Env) -> None:
    assert env.lease().ok
    assert_kept(env, env.reap())


@pytest.mark.parametrize("new_stamp", [B, A])
def test_reopened_lane_and_same_second_ambiguity_are_live(
    env: Env, new_stamp: str
) -> None:
    env.write_rows(
        [row("CLAIM"), row("TERMINAL", B, closes=A), row("CLAIM", new_stamp)]
    )
    env.store.add(PROJECT, "worker", new_stamp, "one", ())
    assert_kept(env, env.reap())


def test_relayed_terminal_after_newer_claim_closes_only_its_named_claim(
    env: Env,
) -> None:
    env.write_rows([row("CLAIM"), row("CLAIM", B), row("TERMINAL", B, closes=A)])
    env.store.add(PROJECT, "worker", B, "one", ())
    assert_kept(env, env.reap())


@pytest.mark.parametrize(
    "terminal_stamp", ["2026-10-15T09:00:00Z", "2026-10-15T13:00:00Z"]
)
def test_terminal_clock_order_is_irrelevant(env: Env, terminal_stamp: str) -> None:
    env.write_rows([row("TERMINAL", terminal_stamp, closes=A), row("CLAIM")])
    result = env.reap()
    assert result.ok
    assert result.projects[0].decision == "removed"


@pytest.mark.parametrize(
    "rows",
    [
        [row("CLAIM"), row("TERMINAL", B)],
        [row("TERMINAL", B, closes=A)],
        [row("CLAIM"), row("TERMINAL", B, lane="other", closes=A)],
    ],
)
def test_absent_claim_or_terminal_without_matching_identity_closes_nothing(
    env: Env, rows: list[str]
) -> None:
    env.write_rows(rows)
    assert_kept(env, env.reap())


def test_using_lane_must_quiesce_before_its_terminal(env: Env) -> None:
    # terminal_before_quiesce is a producer obligation: while using a stack, its lease is OPEN.
    # A detached command retaining its own live registered use must not be closed by another lane.
    env.write_rows(
        [row("CLAIM"), row("CLAIM", B, lane="detached"), row("TERMINAL", B, closes=A)]
    )
    env.store.add(PROJECT, "detached", B, "one", ())
    assert_kept(env, env.reap())


def test_live_lane_on_same_carrier_ticket_does_not_own_this_stack(env: Env) -> None:
    env.write_rows(
        [row("CLAIM"), row("TERMINAL", B, closes=A), row("CLAIM", B, lane="other")]
    )
    result = env.reap()
    assert result.ok
    assert result.projects[0].decision == "removed"


def test_stale_ledger_view_refuses_before_docker(env: Env) -> None:
    os.utime(env.ledger, (NOW.timestamp() - 901, NOW.timestamp() - 901))
    result = env.reap()
    assert result.decision == "refused"
    assert "stale" in result.alarms[0]
    assert not any(call[0] == "docker" for call in env.calls)
    assert env.store.path(PROJECT, "json").exists()


def test_transient_failure_is_retried_in_the_same_pass(env: Env) -> None:
    env.failures = 1
    result = env.reap()
    assert result.ok
    assert result.projects[0].decision == "removed"
    assert len(env.downs) == 2


def test_spent_retries_alarm_append_friction_and_lift_fence(env: Env) -> None:
    env.failures = 5
    result = env.reap(retries=3)
    assert not result.ok
    assert result.projects[0].decision == "failed"
    assert len(env.downs) == 3
    assert result.alarms
    assert any(
        " | FRICTION | " in r and "cost=est ~0.1 lane-hours" in r
        for r in result.ledger_rows
    )
    assert env.rows == list(result.ledger_rows)
    assert env.store.path(PROJECT, "json").exists()
    assert not env.store.path(PROJECT, "fence").exists()


def test_killed_pass_preserves_lease_and_fence_and_alarms(env: Env) -> None:
    before = env.store.path(PROJECT, "json").read_bytes()
    env.abort = True
    result = env.reap()
    assert result.decision == "aborted"
    assert result.alarms
    assert env.store.path(PROJECT, "json").read_bytes() == before
    assert env.store.path(PROJECT, "fence").exists()
    assert any(" | FRICTION | " in r for r in env.rows)
    env.abort = False
    assert env.reap().projects[0].decision == "removed"


def test_orphaned_removal_is_run_again_until_gone(env: Env) -> None:
    env.store.path(PROJECT, "fence").touch()
    result = env.reap()
    assert result.ok
    assert result.projects[0].decision == "removed"
    assert len(env.downs) == 1
    assert not env.store.path(PROJECT, "fence").exists()


def test_orphaned_removal_waits_for_daemon_before_retry(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    env.store.path(PROJECT, "fence").touch()
    waited: list[float] = []

    def sleep(seconds: float) -> None:
        waited.append(seconds)
        env.projects.pop(PROJECT)

    monkeypatch.setattr(f"{reaper.__name__}.time.sleep", sleep)
    result = env.reap(fence_wait_s=1)
    assert waited
    assert result.projects[0].decision == "gone"
    assert not env.downs
    assert not env.store.path(PROJECT, "fence").exists()


def test_declared_project_is_never_judged(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    env.census.write_text(
        f"lanes:\n  dev:\n    compose_project: {PROJECT}\n", encoding="utf-8"
    )

    def forbidden(*args: object) -> tuple[str, ...]:
        raise AssertionError("protected leases must not be judged")

    monkeypatch.setattr(reaper, "open_lanes", forbidden)
    assert_kept(env, env.reap(), "refused-protected")
    assert env.lease().decision == "refused"


def test_lane_named_container_is_protected(env: Env) -> None:
    env.projects[PROJECT] = ["declared-runtime"]
    assert_kept(env, env.reap(), "refused-protected")


def test_foreign_user_stack_is_protected(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Build a synthetic foreign home using path components, without embedding a home path.
    env.config[PROJECT] = str(Path(os.sep) / "home" / "other-account" / "compose.yaml")
    monkeypatch.setattr(reaper, "acting_user", lambda _host: "acting-account")
    assert_kept(env, env.reap(), "refused-protected")


@pytest.mark.parametrize("missing", [False, True])
def test_changed_or_missing_teardown_file_refuses_and_lifts_fence(
    env: Env, missing: bool
) -> None:
    file = env.ledger.parent / "compose.yaml"
    file.write_text("services: {}", encoding="utf-8")
    env.store.add(PROJECT, "worker", A, "two", (file,))
    if missing:
        file.unlink()
    else:
        file.write_text("services: {changed: {}}", encoding="utf-8")
    result = env.reap()
    assert_kept(env, result, "refused-teardown-inputs")
    assert result.alarms
    assert not env.store.path(PROJECT, "fence").exists()


def test_unchanged_teardown_files_are_explicit_down_inputs(env: Env) -> None:
    file = env.ledger.parent / "compose.yaml"
    file.write_text("services: {}", encoding="utf-8")
    env.store.add(PROJECT, "worker", A, "two", (file,))
    env.config[PROJECT] = str(file)
    assert env.reap().ok
    assert env.downs == [["docker", "compose", "-p", PROJECT, "-f", str(file), "down"]]


def test_interval_plus_cap_must_fit_deadline(env: Env) -> None:
    result = env.reap(interval_min=21)
    assert result.decision == "refused"
    assert "deadline" in result.alarms[0]
    assert env.downs == []
    assert PROJECT in env.projects


def test_unreadable_lease_directory_refuses_entire_pass(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = Path.iterdir

    def iterdir(path: Path) -> Iterator[Path]:
        if path == env.store.directory:
            raise PermissionError("lease directory denied")
        return original(path)

    monkeypatch.setattr(Path, "iterdir", iterdir)
    result = env.reap()
    assert result.decision == "refused"
    assert "denied" in result.alarms[0]
    assert not env.downs
    assert PROJECT in env.projects


@pytest.mark.parametrize(
    "failure", [OSError("unreadable"), ClaimStoreIncompleteError("missing archive")]
)
def test_unreadable_claim_store_refuses_entire_pass(
    env: Env, monkeypatch: pytest.MonkeyPatch, failure: Exception
) -> None:
    def sources(*args: object, **kwargs: object) -> list[object]:
        raise failure

    monkeypatch.setattr(reaper, "window_sources", sources)
    result = env.reap()
    assert result.decision == "refused"
    assert result.alarms
    assert not env.downs


def test_missing_ledger_refuses_pass(env: Env) -> None:
    env.ledger.unlink()
    result = env.reap()
    assert result.decision == "refused"
    assert not env.downs


@pytest.mark.parametrize("listing", ["broken-json", "{}", '[{"Status":"running"}]'])
def test_malformed_docker_inventory_does_not_clear_leases(
    env: Env, listing: str
) -> None:
    env.bad_listing = listing
    result = env.reap()
    assert result.decision == "refused"
    assert result.alarms
    assert not env.downs
    assert env.store.path(PROJECT, "json").exists()


def test_failed_docker_inventory_refuses_pass(env: Env) -> None:
    env.list_rc = 1
    result = env.reap()
    assert result.decision == "refused"
    assert not env.downs


def test_gone_stack_clears_data_and_fence_but_preserves_lock_inode(env: Env) -> None:
    lock = env.store.path(PROJECT, "lock")
    inode = lock.stat().st_ino
    env.store.path(PROJECT, "fence").touch()
    env.projects.clear()
    result = env.reap()
    assert result.ok
    assert result.projects[0].decision == "gone"
    assert not env.store.path(PROJECT, "json").exists()
    assert not env.store.path(PROJECT, "fence").exists()
    assert lock.stat().st_ino == inode


def test_generation_replaces_leases_and_same_identity_is_idempotent(env: Env) -> None:
    lock_inode = env.store.path(PROJECT, "lock").stat().st_ino
    assert env.lease().ok
    assert env.lease().ok
    assert len(env.store.read(PROJECT).leases) == 2
    env.store.add(PROJECT, "fresh", B, "two", ())
    record = env.store.read(PROJECT)
    assert record.generation == "two"
    assert [lease.lane for lease in record.leases] == ["fresh"]
    assert env.store.path(PROJECT, "lock").stat().st_ino == lock_inode


def test_unreadable_compose_file_leaves_previous_generation_intact(env: Env) -> None:
    before = env.store.path(PROJECT, "json").read_bytes()
    with pytest.raises(OSError, match="missing"):
        env.store.add(
            PROJECT, "reuser", B, "two", (env.ledger.parent / "missing.yaml",)
        )
    assert env.store.path(PROJECT, "json").read_bytes() == before


@pytest.mark.parametrize("project", ["../escape", "Upper", "a/b", "a\n", ""])
def test_project_name_cannot_escape_store(env: Env, project: str) -> None:
    with pytest.raises(ValueError, match="invalid compose project"):
        env.store.add(project, "worker", A, "one", ())


def test_last_run_and_status_include_outcomes(env: Env) -> None:
    result = env.reap()
    record = json.loads(
        (env.store.directory / "last-run.json").read_text(encoding="utf-8")
    )
    assert record == {
        "at": "2026-10-15T12:00:00Z",
        "decision": "completed",
        "removed": [PROJECT],
        "alarms": [],
    }
    assert result.ledger_rows == tuple(env.rows)
    assert "actor=script:lab-stack-reaper | model=none" in env.rows[-1]
    assert "removed=1" in env.rows[-1]


def test_ledger_append_failure_is_reported_to_timer(env: Env) -> None:
    env.append_rc = 1
    result = env.reap(execute=False)
    assert not result.ok
    assert result.errors
    record = json.loads(
        (env.store.directory / "last-run.json").read_text(encoding="utf-8")
    )
    assert record["alarms"]
    assert record["decision"] == "alarmed"


def test_explicit_docker_host_is_used_for_every_docker_command(env: Env) -> None:
    assert env.reap(docker_host="ssh://lab-host").ok
    calls = [c for c in env.calls if c[0] == "docker"]
    assert calls
    assert all(c[1:3] == ["-H", "ssh://lab-host"] for c in calls)


def test_cli_exit_codes_and_default_dry_run(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "omnimarket.nodes.node_lab_stack_reaper_effect.handlers.handler_lab_stack_reaper_cli.HandlerLabStackReaper",
        lambda: HandlerLabStackReaper(env.run),
    )
    # CLI uses wall time; keep its scratch ledger fresh.
    os.utime(env.ledger, None)
    args = ["--lease-dir", str(env.store.directory), "--census", str(env.census)]
    assert main(["reap", "--ledger", str(env.ledger), *args]) == 0
    assert env.downs == []
    env.store.path(PROJECT, "fence").touch()
    assert (
        main(["lease", "--project", PROJECT, "--lane", "worker", "--claim", A, *args])
        == 3
    )
    assert (
        main(["reap", "--ledger", str(env.ledger), "--interval-min", "21", *args]) == 1
    )
    with pytest.raises(SystemExit) as error:
        main(["reap"])
    assert error.value.code == 2


def test_shared_network_is_protected_even_without_attached_peer_containers(
    env: Env,
) -> None:
    env.projects["peer"] = []
    env.config["peer"] = str(env.ledger.parent / "peer-compose.yaml")
    env.networks[PROJECT] = {"default": {"name": "shared", "external": False}}
    env.networks["peer"] = {"shared": {"name": "shared", "external": True}}
    result = env.reap()
    assert_kept(env, result, "refused-teardown-inputs")
    assert "also expected by peer" in result.projects[0].reason
    assert result.alarms
    assert not env.store.path(PROJECT, "fence").exists()


def test_shared_external_network_is_not_removed_by_down(env: Env) -> None:
    env.networks[PROJECT] = {"shared": {"name": "shared", "external": True}}
    result = env.reap()
    assert result.ok
    assert result.projects[0].decision == "removed"


def test_exclusively_owned_network_can_be_removed(env: Env) -> None:
    env.networks[PROJECT] = {"default": {"name": "private", "external": False}}
    result = env.reap()
    assert result.ok
    assert result.projects[0].decision == "removed"


def test_unreadable_network_definition_refuses_removal(env: Env) -> None:
    env.config_rc = 1
    result = env.reap()
    assert_kept(env, result, "refused-teardown-inputs")
    assert result.alarms
    assert not env.store.path(PROJECT, "fence").exists()


def test_unrelated_recorded_compose_file_refuses_removal(env: Env) -> None:
    recorded = env.ledger.parent / "unrelated.yaml"
    recorded.write_text("services: {}", encoding="utf-8")
    env.store.add(PROJECT, "worker", A, "two", (recorded,))
    env.config[PROJECT] = str(env.ledger.parent / "actual.yaml")
    result = env.reap()
    assert_kept(env, result, "refused-teardown-inputs")
    assert "differ" in result.projects[0].reason
    assert result.alarms


def test_orphaned_fence_with_live_lease_is_waited_and_kept(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert env.lease().ok
    env.store.path(PROJECT, "fence").touch()
    waited: list[float] = []
    monkeypatch.setattr(f"{reaper.__name__}.time.sleep", waited.append)
    result = env.reap(fence_wait_s=0.001)
    assert waited
    assert_kept(env, result)
    assert env.store.path(PROJECT, "fence").exists()


def test_sigkill_leaves_leases_for_recovery_and_next_pass_alarms(env: Env) -> None:
    import signal

    before = env.store.path(PROJECT, "json").read_bytes()
    child = os.fork()
    if child == 0:

        def killed_runner(argv: Sequence[str]) -> Completed:
            if argv[-1] == "down":
                os.kill(os.getpid(), signal.SIGKILL)
            return env.run(argv)

        HandlerLabStackReaper(killed_runner).handle(env.request())
        os._exit(99)
    _, status = os.waitpid(child, 0)
    assert os.WIFSIGNALED(status)
    assert os.WTERMSIG(status) == signal.SIGKILL
    assert env.store.path(PROJECT, "json").read_bytes() == before
    assert env.store.path(PROJECT, "fence").exists()
    pending = json.loads(
        (env.store.directory / "last-run.json").read_text(encoding="utf-8")
    )
    assert pending["decision"] == "running"
    result = env.reap()
    assert result.projects[0].decision == "removed"
    assert result.alarms
    assert "previous pass was killed" in result.alarms[0]
    assert any(" | FRICTION | " in r for r in env.rows)


def test_lock_failure_alarms_without_removal(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    @contextmanager
    def denied(store: LeaseStore, project: str) -> Iterator[None]:
        raise PermissionError("project lock denied")
        yield

    monkeypatch.setattr(LeaseStore, "lock", denied)
    result = env.reap()
    assert result.decision == "refused"
    assert "lock denied" in result.alarms[0]
    assert not env.downs
    assert env.store.path(PROJECT, "json").exists()


def test_lease_directory_default_uses_environment_then_path_home(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ONEX_LAB_STACK_LEASE_DIR", str(env.store.directory))
    assert request_models.default_lease_dir() == env.store.directory
    monkeypatch.delenv("ONEX_LAB_STACK_LEASE_DIR")
    monkeypatch.setattr(Path, "home", lambda: env.ledger.parent)
    assert (
        request_models.default_lease_dir()
        == env.ledger.parent / ".omninode" / "lab-stack-leases"
    )


def test_fresh_snapshot_recovers_the_installed_stale_ledger_failure(env: Env) -> None:
    payload = json.dumps(
        {
            "at": NOW.isoformat(),
            "census": env.census.read_text(),
            "sources": [{"name": "ledger.md", "text": env.ledger.read_text()}],
        }
    )
    env.census.unlink()  # The scheduled pass must use its snapshot, not a host-volume open.
    os.utime(env.ledger, (0, 0))
    assert not env.reap().ok
    env.calls.clear()

    def run(argv: Sequence[str]) -> Completed:
        if list(argv) == ["read-ledger-host"]:
            return Completed(0, payload, "")
        return env.run(argv)

    result = HandlerLabStackReaper(run).handle(
        env.request(ledger_snapshot_command=("read-ledger-host",))
    )
    assert result.ok, result.line()
    assert env.downs
    appends = [call for call in env.calls if "--append" in call]
    assert all(str(env.ledger) in call for call in appends)


@pytest.mark.parametrize(
    "payload",
    [
        "{}",
        "[]",
        "not json",
        json.dumps({"at": "2026-10-01T00:00:00+00:00", "sources": []}),
        json.dumps({"at": NOW.isoformat(), "sources": []}),
        json.dumps(
            {"at": NOW.isoformat(), "sources": [{"name": "../escape", "text": "x"}]}
        ),
    ],
)
def test_unreadable_snapshot_never_falls_back_to_a_stale_file(
    env: Env, payload: str
) -> None:
    def run(argv: Sequence[str]) -> Completed:
        if list(argv) == ["read-ledger-host"]:
            return Completed(0, payload, "")
        return env.run(argv)

    result = HandlerLabStackReaper(run).handle(
        env.request(ledger_snapshot_command=("read-ledger-host",))
    )
    assert not result.ok
    assert not env.downs
    assert not any(call[0] == "docker" for call in env.calls)


HARNESS_ID = "f" * 64


def harness_container(**overrides: Any) -> dict[str, Any]:
    return {
        "Id": HARNESS_ID,
        "Name": "/omn1002-rp-orphan",
        "Created": "2026-10-12T22:01:17Z",
        "Labels": {"com.omninode.omn1002-harness": "1"},
        **overrides,
    }


def run_harness(
    env: Env,
    container: dict[str, Any],
    *,
    change_identity: bool = False,
    fail_remove: bool = False,
    **kwargs: Any,
) -> tuple[ModelLabStackReaperResult, list[list[str]]]:
    # Keep the existing compose positive control, and add the separate labelled-container path.
    removed: list[list[str]] = []
    inspected = 0

    def run(argv: Sequence[str]) -> Completed:
        nonlocal inspected
        args = list(argv)
        if args[:3] == ["docker", "ps", "-aq"]:
            return Completed(
                0, "" if removed and not fail_remove else HARNESS_ID + "\n", ""
            )
        if args[:2] == ["docker", "inspect"]:
            inspected += 1
            value = dict(container)
            if change_identity and inspected > 1:
                value["Labels"] = {
                    "com.omninode.omn1002-harness": "1",
                    "com.omninode.lane": "new",
                }
            return Completed(0, json.dumps(value) + "\n", "")
        if args[:2] == ["docker", "rm"]:
            removed.append(args)
            return Completed(int(fail_remove), "", "failed" if fail_remove else "")
        return env.run(argv)

    root = env.ledger.parent / "ticket-worktrees"
    root.mkdir(exist_ok=True)
    result = HandlerLabStackReaper(run).handle(
        env.request(
            harnesses=True,
            worktree_roots=(root,),
            **kwargs,
        )
    )
    return result, removed


def test_old_labelled_harness_with_gone_ticket_lane_is_removed_by_id(env: Env) -> None:
    result, removed = run_harness(env, harness_container())
    assert result.ok, result.line()
    assert removed == [["docker", "rm", "--force", HARNESS_ID]]
    assert (
        "omn1002-rp-orphan"
        in json.loads((env.store.directory / "last-run.json").read_text())["removed"]
    )


@pytest.mark.parametrize(
    "container",
    [
        harness_container(Labels={}),
        harness_container(Labels={"com.other.omn1002-harness": "1"}),
        harness_container(Name="/example-dogfood-redpanda"),
        harness_container(Name="/omninode-mini-runner-1"),
        harness_container(Name="/declared-runtime"),
        harness_container(
            Labels={
                "com.omninode.omn1002-harness": "1",
                "com.docker.compose.project": "protected-dev",
            }
        ),
        harness_container(Created="2026-10-15T11:45:00Z"),
        harness_container(Created="2026-10-15T12:01:00Z"),
        harness_container(Labels={"com.omninode.generic-harness": "1"}),
    ],
)
def test_harness_safety_boundaries(env: Env, container: dict[str, Any]) -> None:
    result, removed = run_harness(env, container)
    assert result.ok, result.line()
    assert not removed


def test_harness_dry_run_does_not_remove(env: Env) -> None:
    result, removed = run_harness(env, harness_container(), execute=False)
    assert result.ok
    assert not removed
    assert any(
        p.project == "omn1002-rp-orphan" and p.decision == "would-remove"
        for p in result.projects
    )


def test_live_ticket_claim_protects_legacy_harness(env: Env) -> None:
    env.write_rows([row("CLAIM").replace(TICKET_A, TICKET_B)])
    result, removed = run_harness(env, harness_container())
    assert result.ok
    assert not removed


def test_ticket_worktree_protects_unleased_harness(env: Env) -> None:
    root = env.ledger.parent / "ticket-worktrees"
    root.mkdir()
    (root / TICKET_B).mkdir()
    result, removed = run_harness(env, harness_container())
    assert result.ok
    assert not removed


def test_terminal_owner_allows_harness_cleanup(env: Env) -> None:
    container = harness_container(
        Labels={
            "com.omninode.omn1001-harness": "1",
            "com.omninode.lane": "worker",
            "com.omninode.claim": A,
        }
    )
    result, removed = run_harness(env, container)
    assert result.ok
    assert removed


def test_reopened_lane_protects_terminal_harness(env: Env) -> None:
    env.write_rows([row("CLAIM"), row("TERMINAL", B, closes=A), row("CLAIM", B)])
    container = harness_container(
        Labels={
            "com.omninode.omn1001-harness": "1",
            "com.omninode.lane": "worker",
            "com.omninode.claim": A,
        }
    )
    result, removed = run_harness(env, container)
    assert result.ok
    assert not removed


def test_changed_harness_identity_is_not_removed(env: Env) -> None:
    result, removed = run_harness(env, harness_container(), change_identity=True)
    assert result.ok
    assert not removed


def test_failed_harness_removal_is_an_alarm(env: Env) -> None:
    result, removed = run_harness(env, harness_container(), fail_remove=True)
    assert not result.ok
    assert removed


def test_native_docker_runner_is_portable_and_bounded(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[dict[str, Any]] = []

    def run(argv: Sequence[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        assert argv[0] == "docker"  # macOS has neither GNU timeout nor GNU env -C.
        calls.append(kwargs)
        return subprocess.CompletedProcess(argv, 0, "[]", "")

    monkeypatch.setattr(subprocess, "run", run)
    monkeypatch.setenv("COMPOSE_PROJECT_NAME", "dogfood")
    monkeypatch.setenv("DOCKER_HOST", "wrong-daemon")
    HandlerLabStackReaper()._docker(
        env.request(), ["compose", "ls"], time.monotonic() + 10
    )
    assert 0 < calls[0]["timeout"] <= 10
    assert calls[0]["cwd"] == env.store.directory.resolve()
    assert "COMPOSE_PROJECT_NAME" not in calls[0]["env"]
    assert "DOCKER_HOST" not in calls[0]["env"]


def test_runner_compose_project_does_not_raise_an_unleased_alarm(env: Env) -> None:
    env.projects["docker"] = ["omninode-mini-runner-1"]
    result = env.reap()
    assert result.ok, result.line()
    assert "docker" in env.projects
    assert any(
        p.project == "docker" and p.decision == "refused-protected"
        for p in result.projects
    )


def test_inaccessible_ticket_root_refuses_harness_removal(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = Path.iterdir

    def iterdir(path: Path) -> Iterator[Path]:
        if path.name == "ticket-worktrees":
            raise PermissionError("ticket root cannot be read")
        return original(path)

    monkeypatch.setattr(Path, "iterdir", iterdir)
    result, removed = run_harness(env, harness_container())
    assert not result.ok
    assert not removed
    assert any("cannot be read" in alarm for alarm in result.alarms)


def test_claim_older_than_the_existing_window_requires_gone_ticket_lane(
    env: Env,
) -> None:
    # An archive rolled this month can retain an ancient, unclosed CLAIM. The lease reader
    # already excludes it by timestamp; harness ownership must use that same horizon.
    env.write_rows(
        [
            row("CLAIM"),
            row("TERMINAL", B, closes=A),
            row("CLAIM", "2026-09-25T13:28:51Z", lane="old-closeout").replace(
                TICKET_A, TICKET_B
            ),
        ]
    )
    result, removed = run_harness(env, harness_container())
    assert result.ok
    assert removed


def test_ancient_claim_does_not_override_a_present_ticket_worktree(env: Env) -> None:
    env.write_rows(
        [
            row("CLAIM"),
            row("TERMINAL", B, closes=A),
            row("CLAIM", "2026-09-25T13:28:51Z", lane="old-closeout").replace(
                TICKET_A, TICKET_B
            ),
        ]
    )
    root = env.ledger.parent / "ticket-worktrees"
    root.mkdir()
    (root / TICKET_B).mkdir()
    result, removed = run_harness(env, harness_container())
    assert result.ok
    assert not removed


def test_snapshot_does_not_make_a_missing_evidence_target_optional(env: Env) -> None:
    result = HandlerLabStackReaper(env.run).handle(
        env.request(
            ledger=None,
            ledger_snapshot_command=("read-ledger-host",),
        )
    )
    assert not result.ok
    assert not env.downs
    assert not env.calls


@pytest.mark.parametrize(
    "inventory",
    [
        None,
        [],
        {"host": 1},
        {
            "host": "wrong-host",
            "at": NOW.isoformat(),
            "roots_checked": 1,
            "entries": [],
        },
        {
            "host": "current-host",
            "at": "2020-01-01T00:00:00+00:00",
            "roots_checked": 1,
            "entries": [],
        },
        {
            "host": "current-host",
            "at": NOW.isoformat(),
            "roots_checked": 0,
            "entries": [],
        },
    ],
)
def test_missing_or_invalid_ticket_inventory_refuses_before_docker(
    env: Env,
    monkeypatch: pytest.MonkeyPatch,
    inventory: Any,
) -> None:
    import socket

    monkeypatch.setattr(socket, "gethostname", lambda: "current-host")
    payload = json.dumps(
        {
            "at": NOW.isoformat(),
            "census": env.census.read_text(),
            "sources": [{"name": "ledger.md", "text": env.ledger.read_text()}],
            "ticket_inventory": inventory,
        }
    )

    def run(argv: Sequence[str]) -> Completed:
        if list(argv) == ["read-ledger-host"]:
            return Completed(0, payload, "")
        return env.run(argv)

    result = HandlerLabStackReaper(run).handle(
        env.request(
            ledger_snapshot_command=("read-ledger-host",),
            ticket_inventory_required=True,
            worktree_roots=(env.ledger.parent,),
        )
    )
    assert not result.ok
    assert not any(call[0] == "docker" for call in env.calls)


@pytest.mark.parametrize(
    ("entries", "removed_expected"), [([], True), ([TICKET_B], False)]
)
def test_fresh_ticket_inventory_preserves_present_lane_without_opening_volume(
    env: Env,
    monkeypatch: pytest.MonkeyPatch,
    entries: list[str],
    removed_expected: bool,
) -> None:
    result, removed = run_harness(
        env, harness_container(), ticket_lane_entries=tuple(entries)
    )
    assert result.ok
    assert bool(removed) is removed_expected


@pytest.mark.parametrize("corrupt_root", [False, True])
def test_ticket_inventory_wire_probe_resolves_registry_symlink_and_never_hides_read_error(
    tmp_path: Path,
    corrupt_root: bool,
) -> None:
    import sys

    physical = tmp_path / "volume/registry"
    physical.mkdir(parents=True)
    extra = tmp_path / "volume/other-worktrees"
    extra.mkdir()
    home = tmp_path / "user"
    (home / "lab-run/runs").mkdir(parents=True)
    alias = home / "registry"
    alias.symlink_to(physical, target_is_directory=True)
    worktrees = physical / "omni_worktrees"
    if corrupt_root:
        worktrees.write_text("not a directory")
    else:
        (worktrees / TICKET_B).mkdir(parents=True)
    done = subprocess.run(
        [sys.executable, "-c", reaper.TICKET_INVENTORY_SCRIPT, str(extra)],
        env={**os.environ, "OMNI_HOME": str(alias), "HOME": str(home)},
        capture_output=True,
        text=True,
        timeout=15,
    )
    if corrupt_root:
        assert done.returncode != 0
        assert not done.stdout.strip()
    else:
        assert done.returncode == 0, done.stderr
        data = json.loads(done.stdout)
        assert data["roots_checked"] == 3
        assert TICKET_B in data["entries"]


def _standing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, text: str) -> Path:
    path = tmp_path / "standing.yaml"
    path.write_text(text, encoding="utf-8")
    monkeypatch.setenv(reaper.STANDING_STACKS_ENV, str(path))
    return path


def test_standing_infrastructure_stack_is_protected_without_an_alarm(
    env: Env, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The hosts' own stacks (mirrors, caches, the gateway) are never leased. Undeclared, each
    # raised an unleased alarm every pass and held the unit at Result=exit-code.
    unprotected = env.reap()
    assert not any(p.project == "example-mirror" for p in unprotected.projects)
    env.projects["example-mirror"] = ["example-mirror"]
    assert not env.reap().ok
    _standing(
        tmp_path,
        monkeypatch,
        "schema: lab-standing-stacks.v1\nprojects:\n  - {project: example-mirror, hosts: [h901]}\n",
    )
    assert "example-mirror" in reaper.standing_projects()
    env.rows.clear()
    result = env.reap()
    assert result.ok, result.line()
    assert "example-mirror" in env.projects
    assert not any(p.project == "example-mirror" for p in result.projects)
    assert not any("example-mirror" in alarm for alarm in result.alarms)
    assert not any(" | FRICTION | " in r for r in env.rows)


def test_no_standing_declaration_declares_no_standing_stacks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(reaper.STANDING_STACKS_ENV, raising=False)
    monkeypatch.delenv(reaper.OVERLAY_ROOTS_ENV, raising=False)
    assert reaper.standing_projects() == set()


def test_the_standing_declaration_is_found_under_an_overlay_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    declaration = (
        tmp_path / "overlays/node_lab_stack_reaper_effect/standing_stacks.yaml"
    )
    declaration.parent.mkdir(parents=True)
    declaration.write_text(
        "schema: lab-standing-stacks.v1\nprojects:\n  - {project: rooted-stack}\n",
        encoding="utf-8",
    )
    monkeypatch.delenv(reaper.STANDING_STACKS_ENV, raising=False)
    monkeypatch.setenv(
        reaper.OVERLAY_ROOTS_ENV,
        f"{tmp_path / 'missing'}{os.pathsep}{tmp_path / 'overlays'}",
    )
    assert reaper.standing_projects() == {"rooted-stack"}


def test_an_unreadable_standing_stack_declaration_refuses_the_pass(
    env: Env, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _standing(tmp_path, monkeypatch, "schema: other\nprojects: []\n")
    result = env.reap()
    assert result.decision == "refused"
    assert env.downs == []
    assert any("invalid standing stack declaration" in alarm for alarm in result.alarms)


def test_a_container_gone_between_listing_and_inspection_does_not_refuse(
    env: Env,
) -> None:
    # A pass was refused on "no such object" for a container a lane removed
    # between `docker ps -aq` and `docker inspect`.
    gone = "e" * 64
    listings = 0

    def run(argv: Sequence[str]) -> Completed:
        nonlocal listings
        args = list(argv)
        if args[:3] == ["docker", "ps", "-aq"]:
            if "status=dead" in args:
                return Completed(0, "", "")
            listings += 1
            first = HARNESS_ID + "\n" + gone + "\n"
            return Completed(0, first if listings == 1 else HARNESS_ID + "\n", "")
        if args[:2] == ["docker", "inspect"]:
            if gone in args:
                return Completed(1, "", f"error: no such object: {gone}")
            return Completed(0, json.dumps(harness_container(Labels={})) + "\n", "")
        return env.run(argv)

    result = HandlerLabStackReaper(run).handle(env.request(harnesses=True))
    assert result.decision == "completed", result.line()
    assert not any("docker inspect failed" in alarm for alarm in result.alarms)


def test_a_dead_container_the_daemon_cannot_inspect_does_not_refuse(env: Env) -> None:
    # Two Dead ids stayed listed and uninspectable, refusing every pass.
    dead = "d" * 64

    def run(argv: Sequence[str]) -> Completed:
        args = list(argv)
        if args[:3] == ["docker", "ps", "-aq"]:
            if "status=dead" in args:
                return Completed(0, dead + "\n", "")
            return Completed(0, HARNESS_ID + "\n" + dead + "\n", "")
        if args[:2] == ["docker", "inspect"]:
            if dead in args:
                return Completed(1, "", f"error: no such object: {dead}")
            return Completed(0, json.dumps(harness_container(Labels={})) + "\n", "")
        return env.run(argv)

    result = HandlerLabStackReaper(run).handle(env.request(harnesses=True))
    assert result.decision == "completed", result.line()


def test_an_inspection_failure_for_a_present_container_still_refuses(env: Env) -> None:
    def run(argv: Sequence[str]) -> Completed:
        args = list(argv)
        if args[:3] == ["docker", "ps", "-aq"]:
            return Completed(0, "" if "status=dead" in args else HARNESS_ID + "\n", "")
        if args[:2] == ["docker", "inspect"]:
            return Completed(1, "", "error: daemon unavailable")
        return env.run(argv)

    result = HandlerLabStackReaper(run).handle(env.request(harnesses=True))
    assert result.decision == "refused"
    assert any("docker inspect failed" in alarm for alarm in result.alarms)


# ---------------------------------------------------- the declared lane projects need no plugin


def _declared_request(
    tmp_path: Path, census: Path | None
) -> ModelLabStackReaperRequest:
    return ModelLabStackReaperRequest(
        "reap", ledger=tmp_path / "ledger.md", census=census
    )


def test_the_census_declares_the_protected_projects(tmp_path: Path) -> None:
    census = tmp_path / "census.yaml"
    census.write_text(
        "lanes:\n  dev:\n    compose_project: protected-dev\n"
        "    services:\n      - name: declared-runtime\n",
        encoding="utf-8",
    )
    projects, names, source = support.load_declared(_declared_request(tmp_path, census))
    assert projects == {"protected-dev"}
    assert "protected-dev" in projects
    assert names == {"declared-runtime"}
    assert source == str(census)


def test_projects_the_host_declares_in_the_environment_are_protected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    census = tmp_path / "census.yaml"
    census.write_text(
        "lanes:\n  dev:\n    compose_project: protected-dev\n", encoding="utf-8"
    )
    monkeypatch.setenv(support.DECLARED_PROJECTS_ENV, "host-declared, other-declared ,")
    projects, _names, _source = support.load_declared(
        _declared_request(tmp_path, census)
    )
    assert {"host-declared", "other-declared"} <= projects


@pytest.mark.parametrize("content", [None, "not: a census\n", "lanes: {}\n"])
def test_a_missing_or_empty_census_refuses_the_declared_set(
    tmp_path: Path, content: str | None
) -> None:
    census = tmp_path / "census.yaml"
    if content is not None:
        census.write_text(content, encoding="utf-8")
    with pytest.raises(ValueError, match="no readable lane census"):
        support.load_declared(_declared_request(tmp_path, census))


def test_the_census_is_found_through_the_environment_then_the_config_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "config"
    manifest = root / support.CONFIG_CENSUS_REL
    manifest.parent.mkdir(parents=True)
    manifest.write_text(
        "lanes:\n  dev:\n    compose_project: from-config-root\n", encoding="utf-8"
    )
    monkeypatch.delenv(support.CENSUS_ENV, raising=False)
    monkeypatch.setenv(support.CONFIG_ROOT_ENV, str(root))
    projects, _names, source = support.load_declared(_declared_request(tmp_path, None))
    assert "from-config-root" in projects
    assert source == str(manifest)
    explicit = tmp_path / "explicit.yaml"
    explicit.write_text(
        "lanes:\n  dev:\n    compose_project: from-env\n", encoding="utf-8"
    )
    monkeypatch.setenv(support.CENSUS_ENV, str(explicit))
    projects, _names, source = support.load_declared(_declared_request(tmp_path, None))
    assert "from-env" in projects
    assert "from-config-root" not in projects
    assert source == str(explicit)


def test_a_compose_file_under_another_users_home_is_foreign() -> None:
    home = Path(os.sep) / "home"
    files = [
        str(home / "other/stack/compose.yaml"),
        str(home / "me/stack/compose.yaml"),
    ]
    assert support.foreign_config_paths(files, "me") == ["a home directory of other"]
    assert support.foreign_config_paths(files, "other") == ["a home directory of me"]
    assert support.foreign_config_paths(["/srv/stack/compose.yaml"], "me") == []


def test_the_ledger_append_command_is_the_configured_writer(tmp_path: Path) -> None:
    ledger = tmp_path / "ledger.md"
    assert support.append_argv(ledger, "row", {})[-3:] == [
        str(ledger),
        "--append",
        "row",
    ]
    assert support.append_argv(ledger, "row", {})[0] == "onex-ledger"
    configured = {support.APPEND_COMMAND_ENV: "python3 -m some.writer"}
    assert support.append_argv(ledger, "row", configured)[:3] == [
        "python3",
        "-m",
        "some.writer",
    ]
    seam = {support.APPEND_TOOL_ENV: "fake_writer.py"}
    assert support.append_argv(ledger, "row", seam)[1:2] == ["fake_writer.py"]
