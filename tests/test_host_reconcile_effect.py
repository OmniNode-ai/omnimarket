# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Host effects exercised with local git repositories and script boundaries."""

import json
import os
import signal
import socket
import subprocess
from pathlib import Path
from typing import Literal

import pytest
import yaml
from omnibase_core.validators.no_unguarded_git_subprocess import scrub_git_location_env
from pydantic import BaseModel

from omnimarket.models.model_host_reconcile import (
    ModelHostReconcileCommand,
    ModelHostReconcileDecisions,
    ModelHostReconcileEvaluateRequest,
    ModelHostReconcileRunResult,
    ModelHostReconcileSlackCommand,
)
from omnimarket.nodes.node_host_reconcile_compute.handlers.handler_host_reconcile_compute import (
    HandlerHostReconcileCompute,
)
from omnimarket.nodes.node_host_reconcile_effect.__main__ import main
from omnimarket.nodes.node_host_reconcile_effect.handlers.adapter_publisher import (
    publish_topics,
)
from omnimarket.nodes.node_host_reconcile_effect.handlers.handler_host_reconcile_effect import (
    HandlerHostReconcileEffect,
)

pytestmark = pytest.mark.unit


class RecordingEvaluator(HandlerHostReconcileCompute):
    def __init__(self) -> None:
        self.requests: list[ModelHostReconcileEvaluateRequest] = []

    def handle(
        self, request: ModelHostReconcileEvaluateRequest
    ) -> ModelHostReconcileDecisions:
        self.requests.append(request)
        return super().handle(request)


class RecordingPublisher:
    def __init__(self, *, raises: bool = False) -> None:
        self.events: list[tuple[str, BaseModel]] = []
        self.raises = raises

    def publish(self, topic: str, event: BaseModel) -> None:
        self.events.append((topic, event))
        if self.raises:
            raise RuntimeError("publisher unavailable")


def handler(
    evaluator: RecordingEvaluator, publisher: RecordingPublisher
) -> HandlerHostReconcileEffect:
    return HandlerHostReconcileEffect(
        evaluator=evaluator,
        publisher=publisher,
        completed_topic=publish_topics()[1],
        slack_topic=publish_topics()[0],
    )


def run_git(path: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(path), *args],
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
        env=scrub_git_location_env(),
    ).stdout.strip()


def setup_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, behind: bool = False
) -> Path:
    root = tmp_path / "workspace"
    root.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    for key in ("SLACK_CHANNEL_ID", "KAFKA_BROKERS", "ONEX_RECONCILE_ALERT_CMD"):
        monkeypatch.delenv(key, raising=False)
    remote = tmp_path / "remote.git"
    run_git(tmp_path, "init", "--bare", "--initial-branch=dev", str(remote))
    seed = tmp_path / "seed"
    seed.mkdir()
    run_git(seed, "init", "--initial-branch=dev")
    run_git(seed, "config", "user.name", "Fixture")
    run_git(seed, "config", "user.email", "fixture")
    (seed / "tracked").write_text("first\n")
    run_git(seed, "add", "tracked")
    run_git(seed, "commit", "-m", "initial")
    run_git(seed, "remote", "add", "origin", str(remote))
    run_git(seed, "push", "origin", "dev")
    initial = run_git(seed, "rev-parse", "HEAD")
    for name in ("omnibase_infra", "omnimarket"):
        clone = root / name
        clone.mkdir()
        run_git(clone, "init", "--initial-branch=dev")
        run_git(clone, "remote", "add", "origin", str(remote))
        run_git(clone, "fetch", "origin", "dev")
        run_git(clone, "reset", "--hard", "origin/dev")
    if behind:
        (seed / "tracked").write_text("second\n")
        run_git(seed, "commit", "-am", "advance")
        run_git(seed, "push", "origin", "dev")
    scripts = root / "omnibase_infra/scripts"
    runtime = scripts / "runtime_build"
    runtime.mkdir(parents=True)
    (runtime / "sibling_clone_manifest.sh").write_text(
        "SIBLING_CLONE_MANIFEST=(omnibase_infra omnimarket)\n"
        "SIBLING_CLONE_MANIFEST_DIST_NAMES=(omnibase-core omnimarket)\n"
    )
    (scripts / "reconcile_privilege_lib.sh").write_text(
        "rp_plan_privileges() { RP_OWNER=fixture; CURRENT_USER=fixture; RUN_AS=(); }\n"
        'rp_surface_owner() { printf "%s\\n" fixture; }\n'
    )
    (runtime / "reconcile_deploy_clones.sh").write_text(
        'printf "clone delegate ran\\n"\n'
        "for repo in omnibase_infra omnimarket; do\n"
        'git -C "$OMNI_HOME/$repo" reset --hard "origin/$RECONCILE_BRANCH"\n'
        "done\n"
    )
    sp = root / ".onex-dispatch-venv/lib/python3.12/site-packages"
    sp.mkdir(parents=True)
    (sp / f"omnibase_core-{'1' if behind else '2'}.dist-info").mkdir()
    (sp / "installed-commit").write_text(initial)
    (root / "omnibase_infra/.venv/lib/python3.12/site-packages").mkdir(parents=True)
    (scripts / "reconcile-workspace-venvs.sh").write_text(
        'printf "venv delegate ran\\n"\n'
        'root="$2"\nsp="$root/.onex-dispatch-venv/lib/python3.12/site-packages"\n'
        'rm -rf "$sp"/omnibase_core-*.dist-info\n'
        'mkdir -p "$sp/omnibase_core-2.dist-info"\n'
        'git -C "$root/omnimarket" rev-parse HEAD > "$sp/installed-commit"\n'
    )
    (scripts / "reconcile_verify_movement.py").write_text("""import json
import sys
from pathlib import Path

root = Path(__file__).resolve().parents[2]
cmd, args = sys.argv[1], sys.argv[2:]
with (root / "verifier.calls").open("a") as log:
    log.write(json.dumps([cmd, *args]) + "\\n")
def option(name):
    return args[args.index(name) + 1]
if cmd == "clone-health":
    clone = Path(option("--clone"))
    unhealthy = (clone / ".unhealthy").exists()
    print(f"{clone}\\t{'UNHEALTHY' if unhealthy else 'HEALTHY'}\\tbroken index")
    sys.exit(2 if unhealthy else 0)
elif cmd == "clone-refusal":
    print("dirty\\trefused paths")
    print("owner\\towner: lane")
    print("owner\\tconsecutive refusals at this index state: 1")
elif cmd == "lock-targets":
    print(json.dumps({"omnibase-core": "2"}))
elif cmd == "observe":
    file = Path(option("--site-packages")) / "installed-commit"
    print(json.dumps({"commits": {"omnimarket": file.read_text().strip() if file.exists() else ""}}))
elif cmd == "floor":
    Path(option("--output")).write_text(json.dumps({"schema": "floor-stub", "argv": args}) + "\\n")
else:
    raise AssertionError(f"unexpected verifier command: {cmd}")
""")
    return root


def command(
    root: Path, mode: Literal["check", "repair"] = "repair", **updates: object
) -> ModelHostReconcileCommand:
    return ModelHostReconcileCommand(
        workspace_root=str(root), target_host=socket.gethostname(), mode=mode
    ).model_copy(update=updates)


def calls(root: Path) -> list[list[str]]:
    return [
        json.loads(line) for line in (root / "verifier.calls").read_text().splitlines()
    ]


@pytest.mark.parametrize("mode", ["check", "repair"])
def test_failures_publish_slack_command_before_completed_event(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: Literal["check", "repair"]
) -> None:
    root = setup_workspace(tmp_path, monkeypatch)
    (root / "omnimarket/.unhealthy").touch()
    monkeypatch.setenv("SLACK_CHANNEL_ID", "test-channel")
    publisher = RecordingPublisher()
    cmd = command(root, mode)
    result = handler(RecordingEvaluator(), publisher).handle(cmd)
    assert result.exit_code == 2
    assert [topic for topic, _ in publisher.events] == list(publish_topics())
    slack = publisher.events[0][1]
    assert isinstance(slack, ModelHostReconcileSlackCommand)
    assert slack.channel == "test-channel"
    assert slack.correlation_id == cmd.correlation_id
    assert slack.idempotency_key == f"host-reconcile|{result.host}|{cmd.correlation_id}"
    message = "\n".join(
        f"{s.surface}: {s.verdict} — {s.detail}" for s in result.surfaces if s.remedy
    )
    assert slack.text == (
        f"*OmniNode workspace reconcile FAILED* ({result.host})\n{message}\n"
        f"receipt: {root / '.onex-workspace-reconcile.json'}\nhost root: {root}"
    )
    assert publisher.events[-1][1] == result


def test_failures_without_alert_configuration_publish_only_completed_event(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = setup_workspace(tmp_path, monkeypatch)
    (root / "omnimarket/.unhealthy").touch()
    publisher = RecordingPublisher()
    result = handler(RecordingEvaluator(), publisher).handle(command(root))
    assert result.exit_code == 2
    assert publisher.events == [(publish_topics()[1], result)]


def test_alert_command_runs_without_slack_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = setup_workspace(tmp_path, monkeypatch)
    (root / "omnimarket/.unhealthy").touch()
    monkeypatch.setenv("SLACK_CHANNEL_ID", "test-channel")
    alert = root / "alert.sh"
    output = root / "alert.txt"
    alert.write_text('printf "%s" "$1" > "' + str(output) + '"\n')
    monkeypatch.setenv("ONEX_RECONCILE_ALERT_CMD", f"bash {alert}")
    monkeypatch.setenv("OMNI_HOME", str(root))
    publisher = RecordingPublisher()
    monkeypatch.setattr(
        "omnimarket.nodes.node_host_reconcile_effect.__main__.ContractEventPublisher",
        lambda: publisher,
    )
    assert main([]) == 2
    result = json.loads(capsys.readouterr().out)
    assert [topic for topic, _ in publisher.events] == [publish_topics()[1]]
    message = "\n".join(
        f"{s['surface']}: {s['verdict']} — {s['detail']}"
        for s in result["surfaces"]
        if s["remedy"]
    )
    assert output.read_text() == (
        f"{message}\nreceipt: {root / '.onex-workspace-reconcile.json'}\nhost root: {root}"
    )


def test_slack_command_is_accepted_by_the_publish_node() -> None:
    from uuid import uuid4

    from omnimarket.nodes.node_slack_publish_effect.models.model_slack_publish import (
        ModelSlackPublish,
    )

    slack = ModelHostReconcileSlackCommand(
        channel="test-channel",
        text="failed",
        idempotency_key="test-key",
        correlation_id=uuid4(),
    )
    accepted = ModelSlackPublish.model_validate_json(slack.model_dump_json())
    assert (
        accepted.model_dump(include=set(ModelHostReconcileSlackCommand.model_fields))
        == slack.model_dump()
    )
    assert (
        ModelHostReconcileSlackCommand.model_validate(
            accepted.model_dump(
                include=set(ModelHostReconcileSlackCommand.model_fields)
            )
        )
        == slack
    )


def test_slack_publication_failure_is_best_effort(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = setup_workspace(tmp_path, monkeypatch)
    (root / "omnimarket/.unhealthy").touch()
    monkeypatch.setenv("SLACK_CHANNEL_ID", "test-channel")
    publisher = RecordingPublisher(raises=True)
    result = handler(RecordingEvaluator(), publisher).handle(command(root))
    assert result.exit_code == 2
    assert [topic for topic, _ in publisher.events] == list(publish_topics())
    assert any(
        "could not publish Slack command: publisher unavailable" in line
        for line in result.diagnostics
    )


@pytest.mark.parametrize("mode", ["check", "repair"])
@pytest.mark.parametrize("behind", [False, True])
def test_run_receipt_lock_and_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mode: Literal["check", "repair"],
    behind: bool,
) -> None:
    root = setup_workspace(tmp_path, monkeypatch, behind=behind)
    evaluator, publisher = RecordingEvaluator(), RecordingPublisher()
    previous = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)}
    cmd = command(root, mode)
    result = handler(evaluator, publisher).handle(cmd)
    assert result.exit_code == (2 if mode == "check" and behind else 0)
    assert result.correlation_id == cmd.correlation_id
    assert not (root / ".onex-reconcile-host.lock").exists()
    assert {sig: signal.getsignal(sig) for sig in previous} == previous
    assert publisher.events == [(publish_topics()[1], result)]
    assert result.floor_stamped is (mode == "repair")
    assert (root / ".onex-workspace-floor.json").exists() is (mode == "repair")
    path = root / ".onex-workspace-reconcile.json"
    text = path.read_text()
    receipt = json.loads(text)
    assert list(receipt) == [
        "schema",
        "generated_at",
        "mode",
        "workspace_root",
        "branch",
        "failures",
        "dispatch_premise_failures",
        "surfaces",
    ]
    assert receipt["workspace_root"] == str(root)
    contract = yaml.safe_load(
        (
            Path(__file__).resolve().parents[1]
            / "src/omnimarket/nodes/node_host_reconcile_effect/contract.yaml"
        ).read_text()
    )
    assert receipt["schema"] == contract["metadata"]["receipt_schema"]
    assert receipt["mode"] == mode
    assert receipt["surfaces"] == [s.model_dump() for s in result.surfaces]
    assert receipt["failures"] == (3 if behind and mode == "check" else 0)
    assert receipt["dispatch_premise_failures"] == receipt["failures"]
    surface_lines = [
        line for line in text.splitlines() if line.startswith('    {"surface"')
    ]
    assert len(surface_lines) == len(result.surfaces)
    assert all(
        list(json.loads(line.rstrip(",")))
        == ["surface", "verdict", "dispatch_premise", "remedy", "owner", "detail"]
        for line in surface_lines
    )
    assert tuple(s.surface for s in result.surfaces) == (
        "clone:omnibase_infra",
        "clone:omnimarket",
        "venv:omnibase-core",
        "venv:omnimarket",
        "venv:gate-purity",
    )
    observed = calls(root)
    assert "verdict" not in [c[0] for c in observed]
    refusals = [c for c in observed if c[0] == "clone-refusal"]
    assert len(refusals) == (2 if behind and mode == "check" else 0)
    if refusals:
        assert all("--record" not in c for c in refusals)
        assert [s.owner for s in result.surfaces[:2]] == ["lane", "lane"]
    if mode == "repair":
        assert "clone delegate ran" in result.diagnostics
        assert "venv delegate ran" in result.diagnostics
        floor = next(c for c in observed if c[0] == "floor")
        assert floor[floor.index("--distribution") + 1] == "omnibase_core=2"
        assert floor[floor.index("--omnimarket-commit") + 1] == run_git(
            root / "omnimarket", "rev-parse", "HEAD"
        )
    else:
        assert "clone delegate ran" not in result.diagnostics
        assert "venv delegate ran" not in result.diagnostics
    assert len(evaluator.requests) == 3
    assert [len(r.facts) for r in evaluator.requests] == [1, 1, 5]
    assert (
        result.surfaces
        == HandlerHostReconcileCompute().handle(evaluator.requests[-1]).surfaces
    )


def test_live_foreign_holder_is_retained(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = setup_workspace(tmp_path, monkeypatch)
    lock = root / ".onex-reconcile-host.lock"
    lock.mkdir()
    holder = f"pid={os.getpid()}\nhost=foreign-host\nstarted_at=2026-10-09T00:00:00Z\nholder=host-reconcile\n"
    (lock / "holder").write_text(holder)
    publisher = RecordingPublisher()
    result = handler(RecordingEvaluator(), publisher).handle(command(root))
    assert result.exit_code == 4
    assert (lock / "holder").read_text() == holder
    assert not (root / ".onex-workspace-reconcile.json").exists()
    assert publisher.events == [(publish_topics()[1], result)]


@pytest.mark.parametrize("failure", ["target", "verifier"])
def test_setup_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    root = setup_workspace(tmp_path, monkeypatch)
    cmd = command(root)
    if failure == "target":
        cmd = cmd.model_copy(update={"target_host": socket.gethostname() + "-other"})
    else:
        (root / "omnibase_infra/scripts/reconcile_verify_movement.py").unlink()
    publisher = RecordingPublisher()
    result = handler(RecordingEvaluator(), publisher).handle(cmd)
    assert result.exit_code == 3
    assert "INDETERMINATE" in result.diagnostics[-1]
    assert not (root / ".onex-reconcile-host.lock").exists()
    assert publisher.events == [(publish_topics()[1], result)]


@pytest.mark.parametrize("mode", ["check", "repair"])
def test_publisher_failure_preserves_exit_code(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: Literal["check", "repair"]
) -> None:
    root = setup_workspace(tmp_path, monkeypatch, behind=True)
    publisher = RecordingPublisher(raises=True)
    result = handler(RecordingEvaluator(), publisher).handle(command(root, mode))
    assert result.exit_code == (2 if mode == "check" else 0)
    assert "publisher unavailable" in result.diagnostics[-1]
    assert len(publisher.events) == 1


def test_unhealthy_refusal_recorded_and_floor_retained(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = setup_workspace(tmp_path, monkeypatch)
    (root / "omnimarket/.unhealthy").touch()
    floor = root / ".onex-workspace-floor.json"
    floor.write_text("previous floor\n")
    evaluator = RecordingEvaluator()
    result = handler(evaluator, RecordingPublisher()).handle(command(root))
    assert result.exit_code == 2
    assert result.surfaces[1].verdict == "UNHEALTHY"
    assert result.surfaces[1].owner == "lane"
    assert "--record" in next(c for c in calls(root) if c[0] == "clone-refusal")
    assert not result.floor_stamped
    assert floor.read_text() == "previous floor\n"
    assert all(c[0] != "floor" for c in calls(root))


def test_unrelated_failures_allow_floor_and_observe_guard_facts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = setup_workspace(tmp_path, monkeypatch)
    scripts = root / "omnibase_infra/scripts"
    gate = root / "omnibase_infra/.venv/lib/python3.12/site-packages"
    (gate / "omnimarket-1.dist-info").mkdir()
    shadow = Path(os.environ["HOME"]) / ".local/bin/onex"
    shadow.parent.mkdir(parents=True)
    shadow.symlink_to(scripts / "wrong-wrapper")
    for directory, content in (
        (scripts / "git-hooks", "source"),
        (root / "scripts/git-hooks", "drift"),
    ):
        directory.mkdir(parents=True)
        (directory / "canonical_clone_guard.sh").write_text(content)
    evaluator = RecordingEvaluator()
    result = handler(evaluator, RecordingPublisher()).handle(command(root))
    assert result.exit_code == 2
    assert result.floor_stamped
    assert [(s.surface, s.verdict) for s in result.surfaces[-3:]] == [
        ("venv:gate-purity", "IMPURE"),
        ("onex-path-shadow", "SHADOWED"),
        ("canonical-guard", "DRIFT"),
    ]
    assert (
        "The dispatch premise IS proven; the unrelated failures do not block onex delegate."
        in result.diagnostics
    )
    assert evaluator.requests[-1].facts[-1].kind == "guard"


def test_missing_delegates_and_venv_preserve_record_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = setup_workspace(tmp_path, monkeypatch)
    cmd = command(
        root,
        clone_delegate=str(root / "missing-clone"),
        venv_delegate=str(root / "missing-venv"),
        dispatch_venv=str(root / "missing-dispatch"),
    )
    result = handler(RecordingEvaluator(), RecordingPublisher()).handle(cmd)
    assert result.exit_code == 2
    assert tuple(s.surface for s in result.surfaces) == (
        "clone-surface",
        "clone:omnibase_infra",
        "clone:omnimarket",
        "venv:dispatch",
        "venv-surface",
        "venv:gate-purity",
    )
    assert not result.floor_stamped


def test_dead_local_holder_reclaimed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = setup_workspace(tmp_path, monkeypatch)
    lock = root / ".onex-reconcile-host.lock"
    lock.mkdir()
    (lock / "holder").write_text(f"pid=999999999\nhost={socket.gethostname()}\n")
    result = handler(RecordingEvaluator(), RecordingPublisher()).handle(
        command(root, "check")
    )
    assert result.exit_code == 0
    assert any("RECLAIMING" in line for line in result.diagnostics)
    assert not lock.exists()


def test_delegate_timeout_releases_lock_and_restores_signals(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = setup_workspace(tmp_path, monkeypatch)
    delegate = root / "slow.sh"
    delegate.write_text("sleep 30\n")
    previous = signal.getsignal(signal.SIGTERM)
    result = handler(RecordingEvaluator(), RecordingPublisher()).handle(
        command(root, clone_delegate=str(delegate), step_timeout_s=1)
    )
    assert result.exit_code == 5
    assert "TIMEOUT" in result.diagnostics[-1]
    assert not (root / ".onex-reconcile-host.lock").exists()
    assert signal.getsignal(signal.SIGTERM) == previous


@pytest.mark.parametrize("argv", [["--unknown"], []])
def test_cli_bad_arguments_and_missing_workspace(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], argv: list[str]
) -> None:
    monkeypatch.delenv("OMNI_HOME", raising=False)
    assert main(argv) == 3
    assert "INDETERMINATE" in capsys.readouterr().err


def test_cli_environment_and_json_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = setup_workspace(tmp_path, monkeypatch)
    monkeypatch.setenv("OMNI_HOME", str(root))
    assert main(["--check", "--verbose", "--branch", "dev"]) == 0
    output = capsys.readouterr()
    result = json.loads(output.out)
    assert result["exit_code"] == 0
    assert result["host"] == socket.gethostname()
    assert all(
        f"[reconcile-host] {line}" in output.err for line in result["diagnostics"]
    )
    monkeypatch.setenv("ONEX_RECONCILE_STEP_TIMEOUT_S", "0")
    assert main([]) == 3
    assert "INDETERMINATE" in capsys.readouterr().err


def test_cli_all_environment_fields(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:

    seen: list[ModelHostReconcileCommand] = []

    def capture(
        self: HandlerHostReconcileEffect, command: ModelHostReconcileCommand
    ) -> ModelHostReconcileRunResult:
        seen.append(command)
        return ModelHostReconcileRunResult(
            exit_code=0, host=command.target_host, correlation_id=command.correlation_id
        )

    monkeypatch.setattr(HandlerHostReconcileEffect, "handle", capture)
    fields = {
        "STEP_TIMEOUT_S": "9",
        "RUN_TIMEOUT_S": "20",
        "MAX_HOLDER_AGE_S": "30",
        "LOCK_STALE_SECONDS": "40",
        "CLONE_DELEGATE": "/clone",
        "VENV_DELEGATE": "/venv",
        "RECEIPT": "/receipt",
        "ALERT_CMD": "alert",
    }
    for key, value in fields.items():
        monkeypatch.setenv("ONEX_RECONCILE_" + key, value)
    monkeypatch.setenv("ONEX_DISPATCH_VENV", "/dispatch")
    monkeypatch.setenv("ONEX_LEDGER_PATH", "/ledger")
    assert main(["--omni-home", "/workspace", "--branch", "branch"]) == 0
    cmd = seen[0]
    assert (cmd.workspace_root, cmd.branch, cmd.mode) == (
        "/workspace",
        "branch",
        "repair",
    )
    assert (
        cmd.step_timeout_s,
        cmd.run_timeout_s,
        cmd.max_holder_age_s,
        cmd.lock_stale_s,
    ) == (9, 20, 30, 40)
    assert (
        cmd.clone_delegate,
        cmd.venv_delegate,
        cmd.receipt,
        cmd.dispatch_venv,
        cmd.ledger,
        cmd.alert_command,
    ) == ("/clone", "/venv", "/receipt", "/dispatch", "/ledger", "alert")
    assert json.loads(capsys.readouterr().out)["correlation_id"] == str(
        cmd.correlation_id
    )


def test_regular_shadow_does_not_resolve_broken_wrapper(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = setup_workspace(tmp_path, monkeypatch)
    wrapper = root / "omnibase_infra/scripts/onex"
    wrapper.symlink_to(wrapper)
    shadow = Path(os.environ["HOME"]) / ".local/bin/onex"
    shadow.parent.mkdir(parents=True)
    shadow.write_text("shadow\n")
    result = handler(RecordingEvaluator(), RecordingPublisher()).handle(command(root))
    assert result.exit_code == 2
    assert result.surfaces[-1].verdict == "SHADOWED"
    assert "a regular file" in result.surfaces[-1].detail
    assert result.floor_stamped


# --------------------------------------------------------------------------- #
# The omnimarket target is the head the venv delegate started from
# --------------------------------------------------------------------------- #
def advance_clone_script(tmp_path: Path, root: Path, label: str) -> str:
    """Shell lines that land a new commit on origin/dev and fast-forward the clone.

    This is the canonical-clone-sync timer moving the omnimarket clone while the
    venv delegate runs.
    """
    seed, clone = tmp_path / "seed", root / "omnimarket"
    return (
        f'printf "{label}\\n" > "{seed}/{label}.txt"\n'
        f'git -C "{seed}" add -A\n'
        f'git -C "{seed}" -c user.name=Fixture -c user.email=fixture commit --quiet -m {label}\n'
        f'git -C "{seed}" push --quiet origin dev\n'
        f'git -C "{clone}" pull --quiet --ff-only origin dev\n'
    )


def install_clone_head_script(root: Path, sp: Path) -> str:
    return (
        f'git -C "{root / "omnimarket"}" rev-parse HEAD > "{sp}/installed-commit"\n'
        f'cp "{sp}/installed-commit" "{root.parent}/installed.txt"\n'
    )


def write_venv_delegate(root: Path, body: str) -> None:
    (root / "omnibase_infra/scripts/reconcile-workspace-venvs.sh").write_text(
        'printf "venv delegate ran\\n"\n'
        'sp="$2/.onex-dispatch-venv/lib/python3.12/site-packages"\n'
        'rm -rf "$sp"/omnibase_core-*.dist-info\n'
        'mkdir -p "$sp/omnibase_core-2.dist-info"\n' + body
    )


def surface_verdict(result: ModelHostReconcileRunResult, surface: str) -> str:
    return next(s.verdict for s in result.surfaces if s.surface == surface)


def floor_commit(root: Path) -> str:
    floor = next(c for c in calls(root) if c[0] == "floor")
    return floor[floor.index("--omnimarket-commit") + 1]


def test_clone_advancing_during_the_venv_delegate_does_not_fail_the_install(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The clone-sync timer may fast-forward omnimarket while the delegate runs.

    The delegate installs the clone HEAD it read when it started, then the clone
    moves on before the readback. Judging the install against the head read
    after the delegate reports DID_NOT_MOVE for a correct install and the floor
    is never stamped.
    """
    root = setup_workspace(tmp_path, monkeypatch, behind=True)
    sp = root / ".onex-dispatch-venv/lib/python3.12/site-packages"
    write_venv_delegate(
        root,
        install_clone_head_script(root, sp)
        + advance_clone_script(tmp_path, root, "raced"),
    )

    result = handler(RecordingEvaluator(), RecordingPublisher()).handle(
        command(root, "repair")
    )

    installed = (tmp_path / "installed.txt").read_text().strip()
    assert run_git(root / "omnimarket", "rev-parse", "HEAD") != installed
    assert surface_verdict(result, "venv:omnimarket") == "MOVED"
    assert result.exit_code == 0
    assert result.floor_stamped is True
    assert floor_commit(root) == installed


def test_install_from_a_head_the_clone_passed_through_mid_run_passes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The clone moves before and after the delegate reads it."""
    root = setup_workspace(tmp_path, monkeypatch, behind=True)
    sp = root / ".onex-dispatch-venv/lib/python3.12/site-packages"
    write_venv_delegate(
        root,
        advance_clone_script(tmp_path, root, "before")
        + install_clone_head_script(root, sp)
        + advance_clone_script(tmp_path, root, "after"),
    )

    result = handler(RecordingEvaluator(), RecordingPublisher()).handle(
        command(root, "repair")
    )

    installed = (tmp_path / "installed.txt").read_text().strip()
    assert installed != run_git(root / "omnimarket", "rev-parse", "HEAD")
    assert surface_verdict(result, "venv:omnimarket") == "MOVED"
    assert result.exit_code == 0
    assert floor_commit(root) == installed


def test_a_delegate_that_installs_nothing_still_fails_when_the_clone_advances(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A no-op delegate is not turned into a pass by the clone moving."""
    root = setup_workspace(tmp_path, monkeypatch, behind=True)
    write_venv_delegate(root, advance_clone_script(tmp_path, root, "raced"))

    result = handler(RecordingEvaluator(), RecordingPublisher()).handle(
        command(root, "repair")
    )

    assert surface_verdict(result, "venv:omnimarket") == "DID_NOT_MOVE"
    assert result.exit_code == 2
    assert result.floor_stamped is False


def test_an_install_off_the_clone_history_still_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A commit the clone never held is not accepted because the clone moved."""
    root = setup_workspace(tmp_path, monkeypatch, behind=True)
    sp = root / ".onex-dispatch-venv/lib/python3.12/site-packages"
    write_venv_delegate(
        root,
        f'printf "{"1" * 40}\\n" > "{sp}/installed-commit"\n'
        + advance_clone_script(tmp_path, root, "raced"),
    )

    result = handler(RecordingEvaluator(), RecordingPublisher()).handle(
        command(root, "repair")
    )

    assert surface_verdict(result, "venv:omnimarket") == "DID_NOT_MOVE"
    assert result.exit_code == 2
    assert result.floor_stamped is False
