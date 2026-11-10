# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Parity of the ssh transport's ``closeout_worktrees`` step with the frozen runner's closeout script (OMN-23502).

The step sends the host the text the omni plugin's ``closeout_script`` built for the recorded lane
(``recorded_runner_steps.RECORDED_CLOSEOUT_SCRIPT``), with only the first line's home-relative run root swapped for
the declared one, and reads back the printed ``LANE-CLOSEOUT`` lines as cells. A change to the moved script or to the
recording fails these.
"""

from __future__ import annotations

import base64

import pytest

from omnimarket.nodes.node_lane_report_relay_effect.handlers.handler_lane_report_relay import (
    closeout_script,
)
from omnimarket.nodes.node_remote_lane_start_effect.adapters.adapter_ssh_transport import (
    ModelHostCommandResult,
    SshLaneHostTransport,
)
from omnimarket.nodes.node_remote_lane_start_effect.tests.recorded_runner_steps import (
    RECORDED_CLOSEOUT_SCRIPT,
)
from omnimarket.nodes.node_remote_lane_start_effect.tests.test_remote_lane_start_staging_parity import (
    RUN_ID,
    SNAPSHOT_OK,
    Recorder,
    as_recorded,
    stage,
    step,
    transport,
)

pytestmark = pytest.mark.unit

LEDGER_NAME = "example-ledger.md"
TOOL_REPO = "example_tools"
LEDGER_ENV = "EXAMPLE_LEDGER_FILE"
# The recorded runner text names the real registry-root variable; built by concatenation so the moved
# files' own guard (tests/nodes/test_remote_lane_nodes_present.py) never sees it spelled out here either.
REGISTRY_ENV = "OMNI" + "_HOME"
TREE = "/srv/example/registry/omni_worktrees/lane-example/repo"
CLOSEOUT_LINE = f"LANE-CLOSEOUT rc=0 tree={TREE} cell=worktree=removed:{TREE} note="


def _with_tool_repo(runner: Recorder) -> SshLaneHostTransport:
    return SshLaneHostTransport(
        ssh_argv=["ssh"],
        host_run_root="/srv/example/runs",
        call_timeout_seconds=30,
        detach_argv=["setsid"],
        closeout_tool_repo=TOOL_REPO,
        closeout_ledger_env=LEDGER_ENV,
        closeout_registry_env=REGISTRY_ENV,
        runner=runner,
    )


def _closeout(answer: ModelHostCommandResult) -> tuple[Recorder, SshLaneHostTransport]:
    runner = Recorder(SNAPSHOT_OK)
    ssh = _with_tool_repo(runner)
    ssh.stage_ledger_snapshot(stage(LEDGER_NAME, base64.b64encode(b"x").decode()))
    runner.answer = answer
    return runner, ssh


def test_the_moved_closeout_script_is_the_recorded_runner_text() -> None:
    assert (
        closeout_script(
            RUN_ID,
            "lane-example",
            "/srv/example/registry",
            LEDGER_NAME,
            tool_repo=TOOL_REPO,
            ledger_env=LEDGER_ENV,
            registry_env=REGISTRY_ENV,
        )
        == RECORDED_CLOSEOUT_SCRIPT
    )


def test_closeout_sends_the_recorded_script_with_the_declared_run_root() -> None:
    runner, ssh = _closeout(
        ModelHostCommandResult(exit_code=0, stdout=CLOSEOUT_LINE + "\n")
    )
    done = ssh.closeout_worktrees(step())
    sent = runner.argvs[-1][-1]
    assert sent.splitlines()[0] == as_recorded(RECORDED_CLOSEOUT_SCRIPT).splitlines()[0]
    assert sent.splitlines()[1:] == RECORDED_CLOSEOUT_SCRIPT.splitlines()[1:]
    assert done.cells == (f"worktree=removed:{TREE}",)
    assert done.error is None
    assert done.unavailable is False


def test_closeout_reports_a_host_without_the_command_as_unavailable() -> None:
    _, ssh = _closeout(
        ModelHostCommandResult(
            exit_code=0,
            stdout="LANE-CLOSEOUT-UNAVAILABLE h-test: no example_tools with onex-lane-closeout beside /srv/example/registry; trees left: repo\n",
        )
    )
    done = ssh.closeout_worktrees(step())
    assert done.unavailable is True
    assert done.cells == ()


def test_closeout_with_no_trees_gives_no_cells_and_no_error() -> None:
    _, ssh = _closeout(
        ModelHostCommandResult(
            exit_code=0, stdout="LANE-CLOSEOUT-NONE no trees under /x\n"
        )
    )
    done = ssh.closeout_worktrees(step())
    assert done.cells == ()
    assert done.error is None
    assert done.unavailable is False


def test_closeout_names_a_refused_tree_and_keeps_the_other_cells() -> None:
    refused = f"LANE-CLOSEOUT rc=2 tree={TREE}-b cell= note=process-inside"
    _, ssh = _closeout(
        ModelHostCommandResult(
            exit_code=0, stdout=CLOSEOUT_LINE + "\n" + refused + "\n"
        )
    )
    done = ssh.closeout_worktrees(step())
    assert done.cells == (f"worktree=removed:{TREE}",)
    assert done.error is not None
    assert f"{TREE}-b: process-inside" in done.error


def test_closeout_names_the_host_failure() -> None:
    _, ssh = _closeout(ModelHostCommandResult(exit_code=70, stderr="mktemp: failed"))
    done = ssh.closeout_worktrees(step())
    assert done.error == "mktemp: failed"


def test_closeout_refuses_without_a_registry_root_and_sends_no_host_command() -> None:
    runner, ssh = _closeout(ModelHostCommandResult(exit_code=0))
    before = len(runner.argvs)
    done = ssh.closeout_worktrees(step(registry_root=None))
    assert done.error is not None
    assert "registry root" in done.error
    assert len(runner.argvs) == before


def test_closeout_without_a_staged_ledger_runs_with_no_ledger_path() -> None:
    runner = Recorder(ModelHostCommandResult(exit_code=0, stdout=""))
    _with_tool_repo(runner).closeout_worktrees(step())
    assert LEDGER_ENV not in runner.argvs[-1][-1]


def test_closeout_without_a_tool_repository_is_left_to_the_runner_and_sends_nothing() -> (
    None
):
    runner = Recorder(ModelHostCommandResult(exit_code=0))
    done = transport(runner).closeout_worktrees(step())
    assert done.error is not None
    assert done.error.startswith("closeout_worktrees: not run by the ssh transport")
    assert runner.argvs == []


def test_a_tool_repository_that_is_not_a_plain_name_is_refused_at_construction() -> (
    None
):
    with pytest.raises(ValueError, match="closeout_tool_repo"):
        SshLaneHostTransport(
            ssh_argv=["ssh"],
            host_run_root="/srv/example/runs",
            call_timeout_seconds=30,
            detach_argv=["setsid"],
            closeout_tool_repo="a b;c",
        )


def test_closeout_without_the_registry_variable_declared_stays_not_run() -> None:
    runner = Recorder(SNAPSHOT_OK)
    ssh = SshLaneHostTransport(
        ssh_argv=["ssh"],
        host_run_root="/srv/example/runs",
        call_timeout_seconds=30,
        detach_argv=["setsid"],
        closeout_tool_repo=TOOL_REPO,
        closeout_ledger_env=LEDGER_ENV,
        runner=runner,
    )
    done = ssh.closeout_worktrees(step())
    assert done.error is not None
    assert "not-run" in done.error or "not run" in done.error
    assert runner.argvs == []


def test_a_registry_variable_that_is_not_an_upper_case_name_is_refused() -> None:
    with pytest.raises(ValueError, match="registry_env"):
        closeout_script(
            RUN_ID,
            "lane-example",
            "/srv/example/registry",
            LEDGER_NAME,
            tool_repo=TOOL_REPO,
            ledger_env=LEDGER_ENV,
            registry_env="not a name",
        )
