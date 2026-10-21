# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-19359 regressions for task mounts, the allowlist and mutation batches.

Use the recording client from the handler tests; no container or host is used.
"""

from __future__ import annotations

from collections.abc import Callable
from importlib import import_module
from typing import Literal, Protocol, cast

import pytest

from omnimarket.nodes.node_push_validation_effect.handlers.handler_focused_test_run_effect import (
    HandlerFocusedTestRunEffect,
)
from omnimarket.nodes.node_push_validation_effect.models.model_focused_test_run_receipt import (
    EnumFocusedTestRunStatus,
)
from omnimarket.nodes.node_push_validation_effect.models.model_focused_test_run_request import (
    ModelFocusedTestRunRequest,
    ModelSourceMutation,
)
from omnimarket.nodes.node_push_validation_effect.protocols.dtl_container_invocation import (
    DtlInvocationRefusedError,
    ModelContainerRunSpec,
    build_docker_run_argv,
    validate_docker_run_argv,
)
from omnimarket.nodes.node_push_validation_effect.protocols.protocol_focused_test_run_client import (
    ProtocolFocusedTestRunClient,
)

pytestmark = pytest.mark.unit


class _RecordingClient(ProtocolFocusedTestRunClient, Protocol):
    calls: list[str]
    files: dict[str, str]
    written: dict[str, str]
    argv: list[str]


# Reuse the existing fake without pulling its test module's unrelated typing
# errors into this strict target. Keep the interface used here explicitly typed.
_support = import_module(
    "tests.nodes.node_push_validation_effect.test_handler_focused_test_run_effect"
)
FakeClient = cast(Callable[[], _RecordingClient], _support.FakeClient)
_request = cast(Callable[..., ModelFocusedTestRunRequest], _support._request)
ROOT = cast(str, _support.ROOT)
CORRELATION = cast(str, _support.CORRELATION)
SOURCE = cast(str, _support.SOURCE)
IMAGE = "dtl-env:omnimarket-9317e28ad468-0f7b315b1ba6"


def _spec() -> ModelContainerRunSpec:
    request = _request()
    return ModelContainerRunSpec(
        task_root=ROOT,
        task_dir=f"{ROOT}/tasks/{request.correlation_id}/fixed-a1",
        correlation_id=request.correlation_id,
        attempt=request.attempt,
        ref_role=request.ref_role,
        image=IMAGE,
        test_node_id=request.test_node_id,
        timeout_seconds=request.timeout_seconds,
    )


@pytest.mark.parametrize("ref_role", ["fixed", "prefix", "mutation"])
@pytest.mark.parametrize("attempt", [1, 9])
def test_each_role_and_retry_mounts_only_its_own_worktree(
    ref_role: Literal["fixed", "prefix", "mutation"], attempt: int
) -> None:
    client = FakeClient()
    receipt = HandlerFocusedTestRunEffect(client).run_sync(
        _request(ref_role=ref_role, attempt=attempt)
    )

    assert receipt.status is EnumFocusedTestRunStatus.COMPLETED
    options = client.argv[: client.argv.index(IMAGE)]
    assert [arg for arg in options if arg in {"-v", "--volume", "--mount"}] == ["-v"]
    assert options[options.index("-v") + 1] == (
        f"{ROOT}/tasks/{CORRELATION}/{ref_role}-a{attempt}:/work:rw"
    )
    validate_docker_run_argv(client.argv, ROOT)


@pytest.mark.parametrize(
    "source",
    [
        f"{ROOT}-other/tasks/{CORRELATION}/fixed-a1",
        f"{ROOT}/tasks-other/{CORRELATION}/fixed-a1",
        f"{ROOT}/tasks/{CORRELATION}",
        f"{ROOT}/tasks/{CORRELATION}/../other/fixed-a1",
    ],
)
def test_validator_rechecks_a_replaced_mount_source(source: str) -> None:
    argv = build_docker_run_argv(_spec())
    validate_docker_run_argv(argv, ROOT)
    # Replace the sole mount: refusal must come from its source, not its count.
    argv[argv.index("-v") + 1] = f"{source}:/work:rw"

    with pytest.raises(DtlInvocationRefusedError, match="mount source"):
        validate_docker_run_argv(argv, ROOT)


@pytest.mark.parametrize(
    "extra",
    [
        ["--runtime", "nvidia"],
        ["--uts", "host"],
        ["--add-host", "host.docker.internal:host-gateway"],
    ],
    ids=lambda extra: " ".join(extra),
)
def test_options_outside_the_allowlist_are_refused(extra: list[str]) -> None:
    argv = build_docker_run_argv(_spec())
    validate_docker_run_argv(argv, ROOT)
    image_at = argv.index(IMAGE)
    argv[image_at:image_at] = extra

    with pytest.raises(DtlInvocationRefusedError, match="option not on the allowlist"):
        validate_docker_run_argv(argv, ROOT)


@pytest.mark.parametrize(("find", "count"), [("guard()", 0), ("run()", 2)])
def test_a_later_non_unique_mutation_refuses_the_entire_batch(
    find: str, count: int
) -> None:
    client = FakeClient()
    path = "src/pkg/mod.py"
    # The first edit is valid; it makes the next edit match zero or two times.
    assert SOURCE.count("guard()") == 1
    assert SOURCE.replace("guard()", "run()").count(find) == count
    receipt = HandlerFocusedTestRunEffect(client).run_sync(
        _request(
            ref_role="mutation",
            mutations=(
                ModelSourceMutation(path=path, find="guard()", replace="run()"),
                ModelSourceMutation(path=path, find=find, replace="disabled()"),
            ),
        )
    )

    assert receipt.status is EnumFocusedTestRunStatus.INFRA_ERROR
    assert f"mutation on {path} matched {count} times; exactly one is required" in (
        receipt.detail
    )
    assert path not in client.written
    assert client.files[path] == SOURCE
    assert "run" not in client.calls
    assert client.argv == []
    assert client.calls[-1] == "teardown"
    assert receipt.teardown_container_absent
    assert receipt.teardown_worktree_absent
