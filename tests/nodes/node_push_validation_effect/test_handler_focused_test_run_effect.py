# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-19359 — HandlerFocusedTestRunEffect, driven by a fake client.

No ssh, git or docker: a recording fake stands in for the lab host so each
branch of the sequence is exercised on its own. The live behaviour (T0
calibration, teardown, no network) is proven on .101 and recorded in the PR.
"""

from __future__ import annotations

from typing import Any

import pytest

from omnimarket.nodes.node_push_validation_effect.handlers.handler_focused_test_run_effect import (
    HandlerFocusedTestRunEffect,
    apply_mutation,
)
from omnimarket.nodes.node_push_validation_effect.models.model_focused_test_run_receipt import (
    EnumFocusedTestRunStatus,
)
from omnimarket.nodes.node_push_validation_effect.models.model_focused_test_run_request import (
    ModelFocusedTestRunRequest,
    ModelSourceMutation,
)
from omnimarket.nodes.node_push_validation_effect.protocols.protocol_focused_test_run_client import (
    FocusedTestRunInfraError,
    FocusedTestRunTaskExistsError,
    ModelContainerRunOutcome,
    ModelEnvImage,
    ModelHostAdmission,
    ModelTeardownOutcome,
    ProtocolFocusedTestRunClient,
)

pytestmark = pytest.mark.unit

ROOT = "/Users/lab/dtl"
CORRELATION = "0f1e2d3c-4b5a-4968-8778-a1b2c3d4e5f6"
SHA = "4c3985a451847571e2cebd06a6edf1940069946e"
NODE = "tests/unit/projection/test_dtl_generated_0f1e2d3c.py::test_refuses"
JUNIT = '<testsuites><testsuite tests="1"><testcase name="test_refuses"/></testsuite></testsuites>'
SOURCE = "def publish():\n    guard()\n    return run()\n"


class FakeClient:
    def __init__(self, **behaviour: Any) -> None:
        self.calls: list[str] = []
        self.files: dict[str, str] = {"src/pkg/mod.py": SOURCE}
        self.written: dict[str, str] = {}
        self.argv: list[str] = []
        self.b = {
            "running": 0,
            "load": 2.0,
            "junit": JUNIT,
            "exit_code": 0,
            "killed": False,
            "teardown": (True, True),
            "prepare_raises": None,
            "run_raises": None,
        }
        self.b.update(behaviour)

    def host_identity(self) -> str:
        return "lab@.101"

    def task_root(self) -> str:
        return ROOT

    def docker_bin(self) -> str:
        return "/usr/local/bin/docker"

    def admission(self) -> ModelHostAdmission:
        self.calls.append("admission")
        return ModelHostAdmission(
            running_task_containers=self.b["running"], load_one_minute=self.b["load"]
        )

    def sweep_orphans(self) -> None:
        self.calls.append("sweep")

    def prepare_worktree(self, repo: str, commit_sha: str, task_dir: str) -> None:
        self.calls.append("prepare")
        if self.b["prepare_raises"]:
            raise self.b["prepare_raises"]

    def ensure_env_image(self, repo: str, task_dir: str) -> ModelEnvImage:
        self.calls.append("image")
        return ModelEnvImage(
            tag="dtl-env:omnimarket-9317e28ad468-0f7b315b1ba6", image_id="sha256:18cb"
        )

    def read_file(self, task_dir: str, path: str) -> str | None:
        return self.written.get(path, self.files.get(path))

    def write_files(self, task_dir: str, files: dict[str, str]) -> None:
        self.calls.append(f"write:{sorted(files)}")
        self.written.update(files)

    def remove_paths(self, task_dir: str, paths: tuple[str, ...]) -> None:
        self.calls.append(f"hide:{list(paths)}")

    def run_container(
        self, argv: list[str], container_name: str, task_dir: str, wall_seconds: int
    ) -> ModelContainerRunOutcome:
        self.calls.append("run")
        self.argv = argv
        if self.b["run_raises"]:
            raise self.b["run_raises"]
        return ModelContainerRunOutcome(
            exit_code=self.b["exit_code"],
            junit_xml=self.b["junit"],
            killed_at_wall_clock=self.b["killed"],
        )

    def teardown(
        self, repo: str, task_dir: str, correlation_id: str, container_name: str
    ) -> ModelTeardownOutcome:
        self.calls.append("teardown")
        container, worktree = self.b["teardown"]
        return ModelTeardownOutcome(
            container_absent=container, worktree_absent=worktree
        )


def _request(**overrides: Any) -> ModelFocusedTestRunRequest:
    values: dict[str, Any] = {
        "repo": "OmniNode-ai/omnimarket",
        "commit_sha": SHA,
        "overlay_files": {
            "tests/unit/projection/test_dtl_generated_0f1e2d3c.py": "def test_refuses(): pass\n"
        },
        "hide_paths": (
            "tests/unit/projection/test_local_evidence_no_broker_publish.py",
        ),
        "test_node_id": NODE,
        "timeout_seconds": 120,
        "correlation_id": CORRELATION,
        "ref_role": "fixed",
        "attempt": 1,
    }
    values.update(overrides)
    return ModelFocusedTestRunRequest(**values)


async def _run(client: FakeClient, **overrides: Any):  # type: ignore[no-untyped-def]
    assert isinstance(client, ProtocolFocusedTestRunClient)
    return await HandlerFocusedTestRunEffect(client=client).handle(
        _request(**overrides)
    )


async def test_a_clean_run_is_completed_with_its_junit_and_teardown() -> None:
    client = FakeClient(exit_code=1)
    receipt = await _run(client)
    assert receipt.status is EnumFocusedTestRunStatus.COMPLETED
    assert receipt.exit_code == 1  # a red test is still a completed run
    assert receipt.junit_xml == JUNIT
    assert receipt.teardown_container_absent
    assert receipt.teardown_worktree_absent
    assert client.calls[:3] == ["admission", "sweep", "prepare"]
    assert client.calls[-2:] == ["run", "teardown"]
    assert client.argv[client.argv.index("-v") + 1] == (
        f"{ROOT}/tasks/{CORRELATION}/fixed-a1:/work:rw"
    )


@pytest.mark.parametrize(("running", "load"), [(2, 1.0), (0, 8.5)])
async def test_admission_refuses_with_host_busy_before_creating_anything(
    running: int, load: float
) -> None:
    client = FakeClient(running=running, load=load)
    receipt = await _run(client)
    assert receipt.status is EnumFocusedTestRunStatus.HOST_BUSY
    assert client.calls == ["admission"]


@pytest.mark.parametrize("find", ["no such text", "    "])
async def test_a_mutation_that_does_not_match_exactly_once_is_refused(
    find: str,
) -> None:
    # "no such text" matches zero times; four spaces match twice in SOURCE.
    assert SOURCE.count(find) != 1
    client = FakeClient()
    receipt = await _run(
        client,
        ref_role="mutation",
        mutations=(ModelSourceMutation(path="src/pkg/mod.py", find=find, replace="x"),),
    )
    assert receipt.status is EnumFocusedTestRunStatus.INFRA_ERROR
    assert "exactly one" in receipt.detail
    assert "run" not in client.calls
    assert client.calls[-1] == "teardown"


async def test_a_mutation_that_matches_once_is_written_before_the_run() -> None:
    client = FakeClient()
    receipt = await _run(
        client,
        ref_role="mutation",
        mutations=(
            ModelSourceMutation(
                path="src/pkg/mod.py", find="    guard()\n", replace=""
            ),
        ),
    )
    assert receipt.status is EnumFocusedTestRunStatus.COMPLETED
    assert client.written["src/pkg/mod.py"] == "def publish():\n    return run()\n"
    assert client.calls.index("write:['src/pkg/mod.py']") < client.calls.index("run")


async def test_a_missing_junit_file_is_an_infra_error_never_a_pass() -> None:
    client = FakeClient(junit=None, exit_code=0)
    receipt = await _run(client)
    assert receipt.status is EnumFocusedTestRunStatus.INFRA_ERROR
    assert "no junit" in receipt.detail


async def test_a_wall_clock_kill_is_an_infra_error() -> None:
    receipt = await _run(FakeClient(killed=True, exit_code=None))
    assert receipt.status is EnumFocusedTestRunStatus.INFRA_ERROR


@pytest.mark.parametrize("teardown", [(False, True), (True, False)])
async def test_a_failed_teardown_check_is_an_infra_error(
    teardown: tuple[bool, bool],
) -> None:
    receipt = await _run(FakeClient(teardown=teardown))
    assert receipt.status is EnumFocusedTestRunStatus.INFRA_ERROR
    assert "teardown" in receipt.detail


async def test_an_ssh_fault_mid_run_still_tears_down() -> None:
    client = FakeClient(run_raises=FocusedTestRunInfraError("ssh failed"))
    receipt = await _run(client)
    assert receipt.status is EnumFocusedTestRunStatus.INFRA_ERROR
    assert client.calls[-1] == "teardown"


async def test_a_rerun_of_the_same_key_is_refused_and_leaves_the_directory_alone() -> (
    None
):
    client = FakeClient(prepare_raises=FocusedTestRunTaskExistsError("exists"))
    receipt = await _run(client)
    assert receipt.status is EnumFocusedTestRunStatus.INFRA_ERROR
    assert "teardown" not in client.calls


def test_apply_mutation_replaces_exactly_one_occurrence() -> None:
    assert apply_mutation("a b a", "b", "c", "f") == "a c a"


@pytest.mark.parametrize(
    "bad",
    [
        {"overlay_files": {"../escape.py": "x"}},
        {"overlay_files": {"/etc/passwd": "x"}},
        {"hide_paths": ("tests/../../x",)},
        {"overlay_files": {".git/hooks/pre-commit": "x"}},
        {"test_node_id": "--help"},
        {"timeout_seconds": 601},
        {"hide_paths": ("tests/unit/projection/test_dtl_generated_0f1e2d3c.py",)},
    ],
)
def test_the_request_refuses_paths_that_escape_the_repository(
    bad: dict[str, Any],
) -> None:
    with pytest.raises(ValueError, match=r"."):
        _request(**bad)
