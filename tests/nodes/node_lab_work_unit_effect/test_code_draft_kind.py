# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-23627 AC15 -- the lab work unit's ``code_draft`` kind.

The diff and the loop result reach the receipt, a timed-out unit's receipt
carries its diff, a capped diff says so, the loop is pinned with
``--backend-id``, and the failure terminal carries the command's correlation
id. A fake ``onex`` on PATH stands in for the loop: it edits a file in its
working directory and writes the loop receipt where ``--state-root`` says.
"""

from __future__ import annotations

import asyncio
import json
import os
import stat
import subprocess
import sys
import uuid
from pathlib import Path

import pytest
from omnibase_core.validators.no_unguarded_git_subprocess import (
    scrub_git_location_env,
)
from omnibase_infra.event_bus.event_bus_inmemory import EventBusInmemory
from pydantic import ValidationError

from omnimarket.lab_work.bus import LabWorkCaller, LabWorkHost
from omnimarket.models.model_delegated_code_edit import ModelDeclaredCheck
from omnimarket.nodes.node_lab_work_unit_effect import (
    EnumLabWorkUnitStatus,
    HandlerHostCapacityAdvertiseEffect,
    HandlerLabWorkUnitEffect,
    ModelLabWorkUnitReceipt,
    ModelLabWorkUnitRequest,
)
from omnimarket.nodes.node_lab_work_unit_effect.models import ModelLabCodeDraft
from omnimarket.nodes.node_lab_work_unit_effect.protocols import (
    HostReading,
    LocalShellLabWorkExecutor,
)

pytestmark = pytest.mark.unit

_FAKE_ONEX = """#!{python}
import json, pathlib, sys, time
argv = sys.argv[1:]
opts = {{argv[i]: argv[i + 1] for i in range(2, len(argv) - 1, 2)}}
pathlib.Path(opts["--state-root"], "..", "argv.json").resolve().write_text(json.dumps(argv))
request = json.loads(pathlib.Path(opts["--request"]).read_text())
pathlib.Path("answer.py").write_text("ANSWER = 42\\n" * 200)
if "SLEEP" in request["task"]:
    time.sleep(30)
out = pathlib.Path(opts["--state-root"], "runs", request["correlation_id"])
out.mkdir(parents=True)
(out / "loop_receipt.json").write_text(json.dumps({{"result": {{
    "loop_run_id": "loop-1", "status": "accepted", "turns": 2,
    "changed_paths": ["answer.py"], "diff_sha256": "ab" * 32}}}}))
"""


def _git(*args: str, cwd: Path) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
        env=scrub_git_location_env(os.environ),
    ).stdout.strip()


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, str]:
    """A local origin with one commit, a fake onex on PATH; returns (root, sha)."""
    origin = tmp_path / "origin" / "OmniNode-ai" / "demo"
    origin.mkdir(parents=True)
    _git("init", "-q", cwd=origin)
    (origin / "base.py").write_text("BASE = 1\n")
    _git("add", "base.py", cwd=origin)
    _git("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "x", cwd=origin)
    (origin.parent / "demo.git").symlink_to(origin / ".git")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake = bin_dir / "onex"
    fake.write_text(_FAKE_ONEX.format(python=sys.executable), encoding="utf-8")
    fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    return tmp_path, _git("rev-parse", "HEAD", cwd=origin)


def _draft(task: str = "write answer.py", **overrides: object) -> ModelLabCodeDraft:
    fields: dict[str, object] = {
        "task": task,
        "writable_globs": ("answer.py",),
        "checks": (ModelDeclaredCheck(name="syntax", argv=("python3", "-V")),),
        "max_turns": 3,
        "backend_id": "local-coder",
        "max_diff_bytes": 100_000,
    }
    fields.update(overrides)
    return ModelLabCodeDraft.model_validate(fields)


def _request(
    sha: str, draft: ModelLabCodeDraft, **overrides: object
) -> ModelLabWorkUnitRequest:
    fields: dict[str, object] = {
        "work_unit_id": "lw-draft-0001",
        "target_host": "h202",
        "repo": "OmniNode-ai/demo",
        "commit_sha": sha,
        "argv": ["onex", "code-edit", "run"],
        "kind": "code_draft",
        "timeout_seconds": 60,
        "lane": "test-lane",
        "code_draft": draft,
    }
    fields.update(overrides)
    return ModelLabWorkUnitRequest.model_validate(fields)


def _run(root: Path, request: ModelLabWorkUnitRequest) -> ModelLabWorkUnitReceipt:
    runner = LocalShellLabWorkExecutor(
        root / "work", url_base=f"file://{root / 'origin'}"
    )
    return runner.run(request, "h202")


def test_completed_unit_returns_the_diff_and_the_loop_result(
    world: tuple[Path, str],
) -> None:
    root, sha = world
    receipt = _run(root, _request(sha, _draft()))
    assert receipt.status is EnumLabWorkUnitStatus.COMPLETED, receipt.detail
    assert "+ANSWER = 42" in receipt.diff
    assert "answer.py" in receipt.diff
    assert receipt.diff_truncated is False
    assert receipt.code_edit_result is not None
    assert receipt.code_edit_result.status.value == "accepted"
    assert receipt.code_edit_result.changed_paths == ("answer.py",)
    assert not (root / "work" / "runs" / "lw-draft-0001").exists()


def test_timed_out_unit_receipt_still_carries_its_diff(world: tuple[Path, str]) -> None:
    root, sha = world
    request = _request(sha, _draft("SLEEP after the edit"), timeout_seconds=2)
    receipt = _run(root, request)
    assert receipt.status is EnumLabWorkUnitStatus.TIMED_OUT
    assert "+ANSWER = 42" in receipt.diff
    assert receipt.code_edit_result is None


def test_diff_over_the_cap_is_cut_and_says_so(world: tuple[Path, str]) -> None:
    root, sha = world
    receipt = _run(root, _request(sha, _draft(max_diff_bytes=200)))
    assert receipt.diff_truncated is True
    assert len(receipt.diff.encode()) <= 200


def test_loop_is_pinned_to_the_drafts_backend_with_an_outside_state_root(
    world: tuple[Path, str],
) -> None:
    root, sha = world
    request = _request(sha, _draft(backend_id="local-omnipc2-chat"))
    _run(root, request)
    argv = json.loads((root / "work" / "state" / "argv.json").read_text())
    assert argv[:2] == ["code-edit", "run"]
    assert argv[argv.index("--backend-id") + 1] == "local-omnipc2-chat"
    state_root = argv[argv.index("--state-root") + 1]
    assert str(root / "work" / "runs") not in state_root
    assert argv[argv.index("--request") + 1].endswith("lw-draft-0001.json")


def test_a_plain_unit_has_no_diff_and_no_loop_result(world: tuple[Path, str]) -> None:
    root, sha = world
    request = ModelLabWorkUnitRequest.model_validate(
        {
            "work_unit_id": "lw-plain-0001",
            "target_host": "h202",
            "repo": "OmniNode-ai/demo",
            "commit_sha": sha,
            "argv": [sys.executable, "-c", "open('x.py','w').write('X=1')"],
            "kind": "test",
            "timeout_seconds": 60,
            "lane": "test-lane",
        }
    )
    receipt = _run(root, request)
    assert receipt.status is EnumLabWorkUnitStatus.COMPLETED
    assert (receipt.diff, receipt.diff_truncated, receipt.code_edit_result) == (
        "",
        False,
        None,
    )


def test_a_draft_must_be_kind_code_draft_and_run_onex(world: tuple[Path, str]) -> None:
    _, sha = world
    with pytest.raises(ValidationError, match="kind code_draft"):
        _request(sha, _draft(), kind="other")
    with pytest.raises(ValidationError, match="onex"):
        _request(sha, _draft(), argv=["python3", "-c", "pass"])


def test_onex_is_an_allowed_executable_for_the_host(world: tuple[Path, str]) -> None:
    _, sha = world
    handler = HandlerLabWorkUnitEffect("h202")
    assert handler.refusal(_request(sha, _draft())) is None


class _Reader:
    def read(self, tools: list[str]) -> HostReading:
        return HostReading(
            cores=8, load1=0.0, mem_available_bytes=2**33, tools=("git",)
        )


class _CrashingRunner:
    def run(
        self, request: ModelLabWorkUnitRequest, host_name: str
    ) -> ModelLabWorkUnitReceipt:
        raise RuntimeError("worker failed")


def test_failure_terminal_carries_the_commands_correlation_id(
    world: tuple[Path, str],
) -> None:
    _, sha = world

    async def scenario() -> tuple[dict[str, object], dict[str, object]]:
        bus = EventBusInmemory(environment="local", group="draft-failure-test")
        await bus.start()
        host = LabWorkHost(
            bus,
            HandlerLabWorkUnitEffect("h202", _CrashingRunner()),
            HandlerHostCapacityAdvertiseEffect(_Reader()),
        )
        caller = LabWorkCaller(bus, wait_slack_seconds=1)
        failures: list[dict[str, object]] = []
        commands: list[dict[str, object]] = []

        async def on_failure(message: object) -> None:
            failures.append(json.loads(message.value))

        async def on_command(message: object) -> None:
            commands.append(json.loads(message.value))

        await bus.subscribe(
            host.topics.failure, on_message=on_failure, group_id="f-probe"
        )
        await bus.subscribe(
            host.topics.command, on_message=on_command, group_id="c-probe"
        )
        await host.start(advertise=False)
        await caller.start()
        try:
            await caller.dispatch(_request(sha, _draft()))
            await host.drain()
        finally:
            await caller.stop()
            await host.stop()
            await bus.close()
        return failures[0], commands[0]

    failure, command = asyncio.run(scenario())
    sent = command["correlation_id"]
    assert uuid.UUID(str(sent))
    assert failure["payload"]["correlation_id"] == str(sent)
    assert failure["correlation_id"] == str(sent)
