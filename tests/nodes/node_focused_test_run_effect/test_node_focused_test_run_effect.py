# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-19458 -- the focused test run as its own bus-reached node on the lab host.

* The contract gives the run its own command topic, and declares no event_bus
  block, so no shared runtime (which has no docker) attaches it.
* The handler is the focused-run sequence bound to a local shell, and refuses a
  repository owner outside its allowed set before anything is fetched.
* The local shell client runs the same scripts with no ssh.
"""

from __future__ import annotations

from importlib import resources

import pytest
import yaml

from omnimarket.nodes.node_focused_test_run_effect import (
    HandlerLabFocusedTestRunEffect,
    LocalShellFocusedRunSubprocess,
)
from omnimarket.nodes.node_push_validation_effect import (
    EnumFocusedTestRunStatus,
    FocusedTestRunInfraError,
    ModelFocusedTestRunRequest,
)
from omnimarket.nodes.node_push_validation_effect.protocols.protocol_focused_test_run_client import (
    ModelContainerRunOutcome,
    ModelEnvImage,
    ModelHostAdmission,
    ModelTeardownOutcome,
)

pytestmark = pytest.mark.unit

_SHA = "4c3985a451847571e2cebd06a6edf1940069946e"
_CID = "f9c814aa-d101-416f-8b5d-605d6e6609f1"
_PASSED_JUNIT = (
    '<testsuites><testsuite name="pytest" errors="0" failures="0" tests="1">'
    '<testcase classname="t" name="test_x"/></testsuite></testsuites>'
)


def _contract(node: str) -> dict[str, object]:
    text = (
        resources.files(f"omnimarket.nodes.{node}")
        .joinpath("contract.yaml")
        .read_text()
    )
    loaded = yaml.safe_load(text)
    assert isinstance(loaded, dict)
    return loaded


def _request(**overrides: object) -> ModelFocusedTestRunRequest:
    fields: dict[str, object] = {
        "repo": "OmniNode-ai/omnimarket",
        "commit_sha": _SHA,
        "overlay_files": {"tests/unit/test_x.py": "def test_x():\n    pass\n"},
        "test_node_id": "tests/unit/test_x.py",
        "timeout_seconds": 60,
        "correlation_id": _CID,
        "ref_role": "fixed",
        "attempt": 1,
    }
    fields.update(overrides)
    return ModelFocusedTestRunRequest.model_validate(fields)


class _RecordingClient:
    """A focused-run client that records the calls and runs nothing."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def host_identity(self) -> str:
        self.calls.append("host_identity")
        return "local:lab"

    def task_root(self) -> str:
        return "/lab/dtl"

    def docker_bin(self) -> str:
        return "/usr/local/bin/docker"

    def admission(self) -> ModelHostAdmission:
        self.calls.append("admission")
        return ModelHostAdmission(running_task_containers=0, load_one_minute=1.0)

    def sweep_orphans(self) -> None:
        self.calls.append("sweep")

    def prepare_worktree(self, repo: str, commit_sha: str, task_dir: str) -> None:
        self.calls.append(f"prepare {repo}")

    def ensure_env_image(self, repo: str, task_dir: str) -> ModelEnvImage:
        return ModelEnvImage(tag="dtl-env:x", image_id="sha256:abc")

    def remove_paths(self, task_dir: str, paths: tuple[str, ...]) -> None:
        return None

    def write_files(self, task_dir: str, files: dict[str, str]) -> None:
        return None

    def read_file(self, task_dir: str, path: str) -> str | None:
        return None

    def run_container(
        self, argv: list[str], container_name: str, task_dir: str, wall_seconds: int
    ) -> ModelContainerRunOutcome:
        self.calls.append("run_container")
        return ModelContainerRunOutcome(exit_code=0, junit_xml=_PASSED_JUNIT)

    def teardown(
        self, repo: str, task_dir: str, correlation_id: str, container_name: str
    ) -> ModelTeardownOutcome:
        self.calls.append("teardown")
        return ModelTeardownOutcome(container_absent=True, worktree_absent=True)


def test_the_run_has_its_own_command_topic_and_the_receipt_topics() -> None:
    ours = _contract("node_focused_test_run_effect")
    push = _contract("node_push_validation_effect")
    dispatch = ours["runtime_dispatch"]
    assert isinstance(dispatch, dict)
    push_dispatch = push["runtime_dispatch"]
    assert isinstance(push_dispatch, dict)
    assert dispatch["command_topic"] != push_dispatch["command_topic"]
    event_bus = push["event_bus"]
    assert isinstance(event_bus, dict)
    terminals = dispatch["terminal_events"]
    assert isinstance(terminals, dict)
    # The live producer of the receipt topics push-validation declares as
    # published and externally consumed.
    assert {terminals["success"], terminals["failure"]} <= set(
        event_bus["publish_topics"]
    )


def test_no_shared_runtime_attaches_the_node() -> None:
    # Runtime auto-wiring attaches only contracts with handler_routing AND
    # event_bus; the shared runtimes have no docker.
    ours = _contract("node_focused_test_run_effect")
    assert "event_bus" not in ours
    assert "handler_routing" in ours


async def test_an_owner_outside_the_allowed_set_is_refused_before_any_fetch() -> None:
    client = _RecordingClient()
    handler = HandlerLabFocusedTestRunEffect(client=client)
    receipt = await handler.handle(_request(repo="someone-else/project"))
    assert receipt.status is EnumFocusedTestRunStatus.INFRA_ERROR
    assert "someone-else" in receipt.detail
    assert client.calls == []


async def test_an_allowed_owner_runs_the_focused_sequence() -> None:
    client = _RecordingClient()
    handler = HandlerLabFocusedTestRunEffect(client=client)
    receipt = await handler.handle(_request())
    assert receipt.status is EnumFocusedTestRunStatus.COMPLETED
    assert receipt.host == "local:lab"
    assert "prepare OmniNode-ai/omnimarket" in client.calls
    assert client.calls[-1] == "teardown"


def test_the_local_shell_runs_the_script_here_without_ssh(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ONEX_DTL_HOST", raising=False)
    client = LocalShellFocusedRunSubprocess()
    result = client._ssh("printf '%s' \"$0\"; cat", stdin=b"-in")
    assert result.stdout == b"/bin/sh-in"
    assert client.host_identity().startswith("local:")


def test_the_local_shell_raises_on_a_failed_checked_command() -> None:
    client = LocalShellFocusedRunSubprocess()
    with pytest.raises(FocusedTestRunInfraError, match="exit code 3"):
        client._ssh("echo boom >&2; exit 3")


def test_exit_255_is_an_ordinary_exit_code_locally() -> None:
    client = LocalShellFocusedRunSubprocess()
    result = client._ssh("exit 255", check=False)
    assert result.returncode == 255


def test_the_local_shell_wall_clock_is_an_infra_error() -> None:
    client = LocalShellFocusedRunSubprocess()
    with pytest.raises(FocusedTestRunInfraError, match="timed out"):
        client._ssh("sleep 5", timeout=1)


def test_scripts_find_docker_helpers_beside_the_docker_binary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # launchd and non-interactive ssh give a PATH without docker's directory,
    # so an image build could not find docker's credential helper.
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    monkeypatch.setenv("ONEX_DTL_DOCKER_BIN", "/opt/dockerish/bin/docker")
    client = LocalShellFocusedRunSubprocess()
    result = client._ssh('printf "%s" "$PATH"')
    assert result.stdout.decode().split(":")[0] == "/opt/dockerish/bin"
