# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-19359 — the per-task container invocation is fixed by the builder.

A model-written test runs in this container, so the container is the sandbox.
On Docker Desktop the engine may bind-mount anything under the shared user
tree, which includes the operator's home; the ONLY thing standing between a
test and that tree is the argument list. These tests pin it:

* the builder emits exactly one ``-v``, the task worktree, under the task root;
* every forbidden form (a second mount in any spelling, a network other than
  none, a capability, a device, privileged mode, a host namespace, an
  environment key outside the closed list) is REFUSED — the positive control:
  each case starts from a list the validator accepts and adds one thing;
* the environment-image build is reproducible (no provenance, fixed epoch)
  and mounts nothing.
"""

from __future__ import annotations

import pytest

from omnimarket.nodes.node_push_validation_effect.protocols.dtl_container_invocation import (
    ALLOWED_ENV_KEYS,
    DTL_LABEL_KEY,
    DtlInvocationRefusedError,
    ModelContainerRunSpec,
    build_docker_run_argv,
    build_env_image_build_argv,
    validate_docker_run_argv,
)

pytestmark = pytest.mark.unit

TASK_ROOT = "/Users/lab/dtl"
CORRELATION = "0f1e2d3c-4b5a-4968-8778-a1b2c3d4e5f6"
IMAGE = "dtl-env:omnimarket-9317e28ad468-0f7b315b1ba6"
NODE_ID = "tests/unit/projection/test_dtl_generated_0f1e2d3c.py::test_refuses"


def _spec(**overrides: object) -> ModelContainerRunSpec:
    values: dict[str, object] = {
        "task_root": TASK_ROOT,
        "task_dir": f"{TASK_ROOT}/tasks/{CORRELATION}/fixed-a1",
        "correlation_id": CORRELATION,
        "attempt": 1,
        "ref_role": "fixed",
        "image": IMAGE,
        "test_node_id": NODE_ID,
        "timeout_seconds": 120,
    }
    values.update(overrides)
    return ModelContainerRunSpec(**values)  # type: ignore[arg-type]


def _with_inserted(argv: list[str], extra: list[str]) -> list[str]:
    """Insert ``extra`` just before the image, where docker reads options."""
    index = argv.index(IMAGE)
    return argv[:index] + extra + argv[index:]


class TestTheBuilderEmitsOneMount:
    def test_exactly_one_mount_whose_source_is_the_task_worktree(self) -> None:
        argv = build_docker_run_argv(_spec())
        mount_flags = [a for a in argv if a in {"-v", "--volume", "--mount"}]
        assert mount_flags == ["-v"]
        source, dest, mode = argv[argv.index("-v") + 1].split(":")
        assert source == f"{TASK_ROOT}/tasks/{CORRELATION}/fixed-a1"
        assert source.startswith(f"{TASK_ROOT}/tasks/")
        assert (dest, mode) == ("/work", "rw")

    def test_the_fixed_rails_are_present(self) -> None:
        argv = build_docker_run_argv(_spec())
        pairs = {argv[i]: argv[i + 1] for i in range(len(argv) - 1)}
        assert pairs["--network"] == "none"
        assert pairs["--user"] == "1000:1000"
        assert pairs["--cap-drop"] == "ALL"
        assert pairs["--security-opt"] == "no-new-privileges"
        assert pairs["--cpus"] == "2"
        assert pairs["--memory"] == "4g"
        assert pairs["--memory-swap"] == "4g"
        assert pairs["--pids-limit"] == "512"
        assert pairs["--label"] == f"{DTL_LABEL_KEY}={CORRELATION}"
        assert "--read-only" in argv
        assert "--rm" in argv
        env_keys = {
            argv[i + 1].split("=", 1)[0] for i, a in enumerate(argv) if a == "--env"
        }
        assert env_keys == set(ALLOWED_ENV_KEYS)

    def test_the_container_name_is_keyed_by_correlation_attempt_and_role(self) -> None:
        argv = build_docker_run_argv(
            _spec(
                attempt=2,
                ref_role="prefix",
                task_dir=f"{TASK_ROOT}/tasks/{CORRELATION}/prefix-a2",
            )
        )
        assert argv[argv.index("--name") + 1] == "dtl-0f1e2d3c-2-prefix"

    def test_the_command_is_a_bounded_focused_pytest(self) -> None:
        argv = build_docker_run_argv(_spec(timeout_seconds=90))
        command = argv[argv.index(IMAGE) + 1 :]
        assert command[:3] == ["timeout", "--kill-after=10", "90"]
        assert command[3:6] == ["/opt/dtl-venv/bin/python", "-m", "pytest"]
        assert "--junitxml=/work/.dtl/junit.xml" in command
        assert command[-1] == NODE_ID

    def test_the_builder_output_passes_its_own_validator(self) -> None:
        validate_docker_run_argv(build_docker_run_argv(_spec()), TASK_ROOT)


class TestTheBuilderRefusesAnythingElse:
    """The positive control: every forbidden form is refused, one at a time."""

    @pytest.mark.parametrize(
        "extra",
        [
            ["-v", "/Users/lab:/host"],
            ["-v", "/var/run/docker.sock:/var/run/docker.sock"],
            ["--volume", "/Users/lab:/host"],
            ["--volume=/Users/lab:/host"],
            ["-v/Users/lab:/host"],
            ["--mount", "type=bind,src=/Users/lab,dst=/host"],
            ["--mount=type=volume,dst=/data"],
            ["--volumes-from", "omninode-gate-runner"],
            ["--privileged"],
            ["--network", "host"],
            ["--network=bridge"],
            ["--net", "host"],
            ["--cap-add", "SYS_ADMIN"],
            ["--cap-add=NET_RAW"],
            ["--device", "/dev/kvm"],
            ["--env", "AWS_SECRET_ACCESS_KEY=x"],
            ["--env=GH_TOKEN=x"],
            ["-e", "GH_TOKEN=x"],
            ["--env-file", "/Users/lab/.env"],
            ["--user", "0:0"],
            ["--pid", "host"],
            ["--ipc", "host"],
            ["--security-opt", "seccomp=unconfined"],
            ["-p", "8080:8080"],
            ["--tmpfs", "/work:rw"],
        ],
        ids=lambda extra: " ".join(extra),
    )
    def test_builder_refuses_a_second_mount(self, extra: list[str]) -> None:
        argv = _with_inserted(build_docker_run_argv(_spec()), extra)
        with pytest.raises(DtlInvocationRefusedError):
            validate_docker_run_argv(argv, TASK_ROOT)

    def test_a_list_without_network_none_is_refused(self) -> None:
        argv = build_docker_run_argv(_spec())
        index = argv.index("--network")
        del argv[index : index + 2]
        with pytest.raises(DtlInvocationRefusedError, match="network"):
            validate_docker_run_argv(argv, TASK_ROOT)

    def test_a_list_without_the_worktree_mount_is_refused(self) -> None:
        argv = build_docker_run_argv(_spec())
        index = argv.index("-v")
        del argv[index : index + 2]
        with pytest.raises(DtlInvocationRefusedError, match="mount"):
            validate_docker_run_argv(argv, TASK_ROOT)

    @pytest.mark.parametrize(
        "task_dir",
        [
            "/Users/lab",
            "/Users/lab/dtl",
            "/Users/lab/dtl/mirrors/x",
            f"{TASK_ROOT}/tasks/../../.ssh",
            f"{TASK_ROOT}/tasks/{CORRELATION}/fixed-a1:/etc",
            "relative/tasks/x",
        ],
    )
    def test_a_mount_source_outside_the_task_root_is_refused(
        self, task_dir: str
    ) -> None:
        with pytest.raises((DtlInvocationRefusedError, ValueError)):
            build_docker_run_argv(_spec(task_dir=task_dir))

    @pytest.mark.parametrize(
        "node_id", ["--help", "-p no:dtl", "tests/../../x.py", "/abs/tests/x.py"]
    )
    def test_a_node_id_that_is_not_a_repo_test_is_refused(self, node_id: str) -> None:
        with pytest.raises((DtlInvocationRefusedError, ValueError)):
            build_docker_run_argv(_spec(test_node_id=node_id))


class TestTheEnvironmentImageBuild:
    def test_the_build_is_reproducible_and_mounts_nothing(self) -> None:
        argv = build_env_image_build_argv(
            docker_bin="/usr/local/bin/docker",
            dockerfile="/Users/lab/dtl/build/Dockerfile.dtl-env",
            context_dir="/Users/lab/dtl/envctx/omnimarket-9317e28ad468",
            test_image="omninode-gate-runner:dtl-09fee4fc4",
            tag=IMAGE,
        )
        assert argv[:2] == ["/usr/local/bin/docker", "build"]
        assert "--provenance=false" in argv
        assert "--sbom=false" in argv
        assert "SOURCE_DATE_EPOCH=0" in argv
        assert "TEST_IMAGE=omninode-gate-runner:dtl-09fee4fc4" in argv
        assert not [a for a in argv if a in {"-v", "--volume", "--mount"}]
        assert argv[-1] == "/Users/lab/dtl/envctx/omnimarket-9317e28ad468"
