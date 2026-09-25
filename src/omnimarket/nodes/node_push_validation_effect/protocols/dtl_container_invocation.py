# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The per-task container invocation for ``run_focused_test_run`` (OMN-19359).

Pure: builds and validates argument lists, runs nothing. The ssh-backed client
passes every ``docker run`` it issues through :func:`validate_docker_run_argv`,
so the rails below hold for every container the operation ever starts, not
only for the lists this module builds.

WHY THE ARGUMENT LIST IS THE SANDBOX. The lab Macs run Docker Desktop, whose
engine runs as root inside its own VM and may bind-mount any directory under
the shared user tree, which includes the operator's home. The model-written
test is untrusted code. Operator ruling 2026-09-23T21:52:08Z decision (2)
accepted, for the first slice, the VM boundary plus exactly these rails:

* exactly ONE bind mount, the task worktree, read-write at ``/work``, whose
  source lies under ``<task root>/tasks/``;
* ``--network none``; ``--user 1000:1000``; ``--cap-drop ALL``;
  ``--security-opt no-new-privileges``; a read-only root with a bounded
  ``/tmp`` tmpfs; 2 CPU, 4 GB, no swap beyond it, 512 pids;
* environment keys from a CLOSED list, so no credential can travel.

The validator is an allowlist over options, not a denylist: an option it does
not know is refused, so a spelling nobody thought of is refused too.
"""

from __future__ import annotations

import re
from pathlib import PurePosixPath
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

DTL_LABEL_KEY = "org.omninode.dtl.correlation_id"
ALLOWED_ENV_KEYS: tuple[str, ...] = ("HOME", "PYTHONPATH", "PYTHONDONTWRITEBYTECODE")
_FIXED_ENV: tuple[str, ...] = (
    "HOME=/tmp",
    "PYTHONPATH=/work/src",
    "PYTHONDONTWRITEBYTECODE=1",
)
CONTAINER_WORKDIR = "/work"
JUNIT_PATH_IN_CONTAINER = "/work/.dtl/junit.xml"
VENV_PYTHON = "/opt/dtl-venv/bin/python"
DEFAULT_DOCKER_BIN = "/usr/local/bin/docker"

# Option -> the value it must carry (None: any value, checked separately).
# Flags without a value map to "".
_REQUIRED_VALUES: dict[str, str] = {
    "--network": "none",
    "--user": "1000:1000",
    "--cap-drop": "ALL",
    "--security-opt": "no-new-privileges",
    "--workdir": CONTAINER_WORKDIR,
    "--cpus": "2",
    "--memory": "4g",
    "--memory-swap": "4g",
    "--pids-limit": "512",
}
_VALUE_OPTIONS: frozenset[str] = frozenset(
    {*_REQUIRED_VALUES, "--name", "--label", "--tmpfs", "--env", "-v"}
)
_BARE_OPTIONS: frozenset[str] = frozenset({"--rm", "--read-only"})
_TMPFS_VALUE = "/tmp:rw,size=512m"

_NODE_ID_PATTERN = re.compile(
    r"^tests/[A-Za-z0-9_./-]+\.py(::[A-Za-z0-9_\[\]().,=-]+)*$"
)
_IMAGE_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._/-]*:[A-Za-z0-9._-]+$")
_NAME_PATTERN = re.compile(r"^dtl-[0-9a-f]{8}-[1-9]-(fixed|prefix|mutation)$")


class DtlInvocationRefusedError(ValueError):
    """An argument list, or a spec for one, breaks a container rail."""


class ModelContainerRunSpec(BaseModel):
    """Everything the builder needs; nothing here can loosen a rail."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    task_root: str = Field(..., min_length=2)
    task_dir: str = Field(..., min_length=2)
    correlation_id: str = Field(..., pattern=r"^[0-9a-f]{8}-[0-9a-f-]{27}$")
    attempt: int = Field(..., ge=1, le=9)
    ref_role: Literal["fixed", "prefix", "mutation"]
    image: str
    test_node_id: str
    timeout_seconds: int = Field(..., ge=1, le=600)
    docker_bin: str = DEFAULT_DOCKER_BIN

    @field_validator("image")
    @classmethod
    def _image_is_a_tag(cls, value: str) -> str:
        if not _IMAGE_PATTERN.fullmatch(value):
            raise DtlInvocationRefusedError(f"image is not a plain tag: {value!r}")
        return value

    @field_validator("test_node_id")
    @classmethod
    def _node_id_is_a_repo_test(cls, value: str) -> str:
        if ".." in value or not _NODE_ID_PATTERN.fullmatch(value):
            raise DtlInvocationRefusedError(f"not a repository test node id: {value!r}")
        return value


def container_name(correlation_id: str, attempt: int, ref_role: str) -> str:
    return f"dtl-{correlation_id[:8]}-{attempt}-{ref_role}"


def _check_mount_source(source: str, task_root: str) -> None:
    root = PurePosixPath(task_root)
    if not root.is_absolute() or ".." in root.parts:
        raise DtlInvocationRefusedError(
            f"task root must be absolute and plain: {task_root!r}"
        )
    path = PurePosixPath(source)
    if not path.is_absolute() or ".." in path.parts or ":" in source:
        raise DtlInvocationRefusedError(
            f"mount source is not a plain absolute path: {source!r}"
        )
    tasks = root / "tasks"
    if tasks not in path.parents or len(path.relative_to(tasks).parts) < 2:
        raise DtlInvocationRefusedError(
            f"mount source {source!r} is not a task worktree under {tasks}"
        )


def build_docker_run_argv(spec: ModelContainerRunSpec) -> list[str]:
    """The one ``docker run`` a focused test run is allowed to issue."""
    _check_mount_source(spec.task_dir, spec.task_root)
    argv = [
        spec.docker_bin,
        "run",
        "--rm",
        "--name",
        container_name(spec.correlation_id, spec.attempt, spec.ref_role),
        "--label",
        f"{DTL_LABEL_KEY}={spec.correlation_id}",
        "--network",
        "none",
        "--user",
        "1000:1000",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
        "--read-only",
        "--tmpfs",
        _TMPFS_VALUE,
        "--workdir",
        CONTAINER_WORKDIR,
        "--cpus",
        "2",
        "--memory",
        "4g",
        "--memory-swap",
        "4g",
        "--pids-limit",
        "512",
    ]
    for env in _FIXED_ENV:
        argv += ["--env", env]
    argv += ["-v", f"{spec.task_dir}:{CONTAINER_WORKDIR}:rw", spec.image]
    argv += [
        "timeout",
        "--kill-after=10",
        str(spec.timeout_seconds),
        VENV_PYTHON,
        "-m",
        "pytest",
        "-p",
        "no:cacheprovider",
        "-q",
        f"--junitxml={JUNIT_PATH_IN_CONTAINER}",
        spec.test_node_id,
    ]
    validate_docker_run_argv(argv, spec.task_root)
    return argv


def validate_docker_run_argv(argv: list[str], task_root: str) -> None:
    """Refuse any ``docker run`` list that breaks a rail. Returns None if clean."""
    if len(argv) < 3 or argv[1] != "run":
        raise DtlInvocationRefusedError("not a docker run argument list")
    seen: dict[str, list[str]] = {}
    index = 2
    while index < len(argv):
        token = argv[index]
        if not token.startswith("-"):
            break  # the image; everything after it is the container command
        if "=" in token:
            raise DtlInvocationRefusedError(
                f"option={{value}} spelling is refused, pass the value apart: {token!r}"
            )
        if token in _BARE_OPTIONS:
            seen.setdefault(token, []).append("")
            index += 1
            continue
        if token not in _VALUE_OPTIONS:
            raise DtlInvocationRefusedError(f"option not on the allowlist: {token!r}")
        if index + 1 >= len(argv):
            raise DtlInvocationRefusedError(f"option {token!r} has no value")
        seen.setdefault(token, []).append(argv[index + 1])
        index += 2
    else:
        raise DtlInvocationRefusedError("no image in the argument list")

    image = argv[index]
    if not _IMAGE_PATTERN.fullmatch(image):
        raise DtlInvocationRefusedError(f"image is not a plain tag: {image!r}")

    for option, required in _REQUIRED_VALUES.items():
        values = seen.get(option, [])
        if values != [required]:
            name = option.lstrip("-")
            raise DtlInvocationRefusedError(
                f"{name} must be given exactly once as {required!r}, got {values!r}"
            )
    for flag in _BARE_OPTIONS:
        if seen.get(flag) != [""]:
            raise DtlInvocationRefusedError(f"{flag} must be given exactly once")
    if seen.get("--tmpfs") != [_TMPFS_VALUE]:
        raise DtlInvocationRefusedError(f"tmpfs must be exactly {_TMPFS_VALUE!r}")

    names = seen.get("--name", [])
    if len(names) != 1 or not _NAME_PATTERN.fullmatch(names[0]):
        raise DtlInvocationRefusedError(
            f"container name is not a dtl task name: {names!r}"
        )
    labels = seen.get("--label", [])
    if len(labels) != 1 or not labels[0].startswith(f"{DTL_LABEL_KEY}="):
        raise DtlInvocationRefusedError(
            f"exactly one {DTL_LABEL_KEY} label: {labels!r}"
        )

    envs = seen.get("--env", [])
    keys = [env.split("=", 1)[0] for env in envs]
    if sorted(keys) != sorted(ALLOWED_ENV_KEYS) or tuple(sorted(envs)) != tuple(
        sorted(_FIXED_ENV)
    ):
        raise DtlInvocationRefusedError(f"environment is not the closed list: {envs!r}")

    mounts = seen.get("-v", [])
    if len(mounts) != 1:
        raise DtlInvocationRefusedError(f"exactly one mount is allowed, got {mounts!r}")
    parts = mounts[0].split(":")
    if len(parts) != 3 or parts[1:] != [CONTAINER_WORKDIR, "rw"]:
        raise DtlInvocationRefusedError(
            f"the mount must be <task dir>:/work:rw: {mounts[0]!r}"
        )
    _check_mount_source(parts[0], task_root)


def build_env_image_build_argv(
    *,
    docker_bin: str,
    dockerfile: str,
    context_dir: str,
    test_image: str,
    tag: str,
) -> list[str]:
    """A reproducible build of one repository's environment image.

    ``--provenance=false`` and a fixed ``SOURCE_DATE_EPOCH`` make a rebuild of
    the same inputs produce the same image id (measured on .101: with the
    provenance attestation, two cached builds gave two ids).
    """
    for value in (tag, test_image):
        if not _IMAGE_PATTERN.fullmatch(value):
            raise DtlInvocationRefusedError(f"not a plain image tag: {value!r}")
    return [
        docker_bin,
        "build",
        "--quiet",
        "--provenance=false",
        "--sbom=false",
        "-f",
        dockerfile,
        "--build-arg",
        "SOURCE_DATE_EPOCH=0",
        "--build-arg",
        f"TEST_IMAGE={test_image}",
        "-t",
        tag,
        context_dir,
    ]


__all__ = [
    "ALLOWED_ENV_KEYS",
    "CONTAINER_WORKDIR",
    "DEFAULT_DOCKER_BIN",
    "DTL_LABEL_KEY",
    "JUNIT_PATH_IN_CONTAINER",
    "DtlInvocationRefusedError",
    "ModelContainerRunSpec",
    "build_docker_run_argv",
    "build_env_image_build_argv",
    "container_name",
    "validate_docker_run_argv",
]
