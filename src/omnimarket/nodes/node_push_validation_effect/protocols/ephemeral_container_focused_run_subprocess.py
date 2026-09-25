# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""EphemeralContainerFocusedRunSubprocess — the ssh-backed focused-run client
(OMN-19359).

Runs on the launching machine and reaches the lab host with plain ``ssh``.
Docker is called by its full path on the host, never through
``DOCKER_HOST=ssh://``, because the lab Macs do not put docker on the
non-interactive ssh PATH (measured on .101 and .105 on 2026-09-23). Every
remote command runs under ``/bin/sh -c`` so the host's login shell does not
matter.

Host layout under the task root (``ONEX_DTL_TASK_ROOT``, required):

* ``mirrors/<owner>__<name>.git`` — one bare mirror per repository, fetched
  anonymously (public repositories only in the first slice);
* ``tasks/<correlation id>/<ref role>-a<attempt>`` — one detached worktree per
  run, the container's only mount, removed after the run whatever happens;
* ``envctx/<key>`` — the two lock inputs an environment image is built from.

Required variables, read when first needed so the handler still constructs at
boot: ``ONEX_DTL_HOST`` (ssh destination), ``ONEX_DTL_TASK_ROOT``,
``ONEX_DTL_TEST_IMAGE`` (the test image tag), and ``ONEX_DTL_ENV_DOCKERFILE``
(the environment Dockerfile's path on the host; only needed to build a missing
image). ``ONEX_DTL_DOCKER_BIN`` defaults to ``/usr/local/bin/docker``.
"""

from __future__ import annotations

import io
import os
import shlex
import subprocess
import tarfile

from omnimarket.nodes.node_push_validation_effect.protocols.dtl_container_invocation import (
    DEFAULT_DOCKER_BIN,
    DTL_LABEL_KEY,
    build_env_image_build_argv,
    validate_docker_run_argv,
)
from omnimarket.nodes.node_push_validation_effect.protocols.protocol_focused_test_run_client import (
    FocusedTestRunInfraError,
    FocusedTestRunTaskExistsError,
    ModelContainerRunOutcome,
    ModelEnvImage,
    ModelHostAdmission,
    ModelTeardownOutcome,
)

_SSH_TIMEOUT_SECONDS = 120
_FETCH_TIMEOUT_SECONDS = 900
_BUILD_TIMEOUT_SECONDS = 1800
_SSH_ERROR_EXIT = 255
_ORPHAN_AGE_MINUTES = 60


class _SshWallClockExceededError(FocusedTestRunInfraError):
    """The local ssh call outlived its wall clock."""


def _required(name: str) -> str:
    try:
        value = os.environ[name]
    except KeyError as exc:
        raise FocusedTestRunInfraError(f"required variable {name} is not set") from exc
    if not value.strip():
        raise FocusedTestRunInfraError(f"required variable {name} is empty")
    return value


def _mirror_name(repo: str) -> str:
    owner, name = repo.split("/", 1)
    return f"{owner}__{name}.git"


class EphemeralContainerFocusedRunSubprocess:
    """Implements ``ProtocolFocusedTestRunClient`` over ssh."""

    def host_identity(self) -> str:
        return _required("ONEX_DTL_HOST")

    def task_root(self) -> str:
        return _required("ONEX_DTL_TASK_ROOT").rstrip("/")

    def docker_bin(self) -> str:
        return os.environ.get("ONEX_DTL_DOCKER_BIN", DEFAULT_DOCKER_BIN)

    # -- transport -----------------------------------------------------------

    def _ssh(
        self,
        script: str,
        *,
        stdin: bytes | None = None,
        timeout: int = _SSH_TIMEOUT_SECONDS,
        check: bool = True,
    ) -> subprocess.CompletedProcess[bytes]:
        argv = [
            "ssh",
            "-o",
            "BatchMode=yes",
            "-o",
            "ConnectTimeout=10",
            self.host_identity(),
            shlex.join(["/bin/sh", "-c", script]),
        ]
        try:
            result = subprocess.run(
                argv, input=stdin, capture_output=True, timeout=timeout, check=False
            )
        except subprocess.TimeoutExpired as exc:
            raise _SshWallClockExceededError(f"ssh timed out after {timeout}s") from exc
        if result.returncode == _SSH_ERROR_EXIT:
            raise FocusedTestRunInfraError(
                "ssh failed: " + result.stderr.decode(errors="replace")[-400:]
            )
        if check and result.returncode != 0:
            raise FocusedTestRunInfraError(
                f"remote command exited {result.returncode}: "
                + result.stderr.decode(errors="replace")[-400:]
            )
        return result

    def _q(self, value: str) -> str:
        return shlex.quote(value)

    # -- protocol ------------------------------------------------------------

    def admission(self) -> ModelHostAdmission:
        docker = self._q(self.docker_bin())
        out = self._ssh(
            f"{docker} ps -q --filter label={DTL_LABEL_KEY} | wc -l; sysctl -n vm.loadavg"
        ).stdout.decode()
        lines = [line.strip() for line in out.splitlines() if line.strip()]
        try:
            running = int(lines[0])
            load = float(lines[1].strip("{} ").split()[0])
        except (IndexError, ValueError) as exc:
            raise FocusedTestRunInfraError(
                f"unreadable admission probe: {out!r}"
            ) from exc
        return ModelHostAdmission(running_task_containers=running, load_one_minute=load)

    def sweep_orphans(self) -> None:
        root = self._q(self.task_root())
        docker = self._q(self.docker_bin())
        self._ssh(
            f"{docker} container prune -f --filter label={DTL_LABEL_KEY} "
            f"--filter until={_ORPHAN_AGE_MINUTES}m >/dev/null; "
            f"mkdir -p {root}/tasks {root}/mirrors {root}/envctx; "
            f"find {root}/tasks -mindepth 1 -maxdepth 1 -type d -mmin +{_ORPHAN_AGE_MINUTES} "
            "-exec rm -rf {} +; "
            f'for m in {root}/mirrors/*.git; do [ -d "$m" ] && git -C "$m" worktree prune; done; true'
        )

    def prepare_worktree(self, repo: str, commit_sha: str, task_dir: str) -> None:
        root = self.task_root()
        mirror = f"{root}/mirrors/{_mirror_name(repo)}"
        url = f"https://github.com/{repo}.git"
        q = self._q
        result = self._ssh(
            "set -e; "
            f"if [ -e {q(task_dir)} ]; then echo 'task dir exists: rerun refused' >&2; exit 3; fi; "
            f"[ -d {q(mirror)} ] || git clone --quiet --bare {q(url)} {q(mirror)}; "
            f"git -C {q(mirror)} cat-file -e {q(commit_sha + '^{commit}')} 2>/dev/null "
            f"|| git -C {q(mirror)} fetch --quiet origin {q(commit_sha)}; "
            f'mkdir -p "$(dirname {q(task_dir)})"; '
            f"git -C {q(mirror)} worktree add --quiet --detach {q(task_dir)} {q(commit_sha)}",
            timeout=_FETCH_TIMEOUT_SECONDS,
            check=False,
        )
        if result.returncode == 3:
            raise FocusedTestRunTaskExistsError(
                f"task dir exists, rerun refused: {task_dir}"
            )
        if result.returncode != 0:
            raise FocusedTestRunInfraError(
                f"worktree preparation exited {result.returncode}: "
                + result.stderr.decode(errors="replace")[-400:]
            )

    def ensure_env_image(self, repo: str, task_dir: str) -> ModelEnvImage:
        q = self._q
        docker = q(self.docker_bin())
        test_image = _required("ONEX_DTL_TEST_IMAGE")
        probe = self._ssh(
            f"shasum -a 256 {q(task_dir + '/uv.lock')} | cut -c1-12; "
            f"{docker} image inspect {q(test_image)} --format '{{{{.Id}}}}'"
        ).stdout.decode()
        lines = [line.strip() for line in probe.splitlines() if line.strip()]
        if len(lines) != 2 or not lines[1].startswith("sha256:"):
            raise FocusedTestRunInfraError(
                f"unreadable lock or test image probe: {probe!r}"
            )
        lock12, test_id12 = lines[0], lines[1].removeprefix("sha256:")[:12]
        name = repo.split("/", 1)[1].lower()
        tag = f"dtl-env:{name}-{lock12}-{test_id12}"
        existing = self._ssh(
            f"{docker} image inspect {q(tag)} --format '{{{{.Id}}}}'", check=False
        )
        if existing.returncode != 0:
            context = f"{self.task_root()}/envctx/{name}-{lock12}-{test_id12}"
            build = build_env_image_build_argv(
                docker_bin=self.docker_bin(),
                dockerfile=_required("ONEX_DTL_ENV_DOCKERFILE"),
                context_dir=context,
                test_image=test_image,
                tag=tag,
            )
            self._ssh(
                "set -e; "
                f"mkdir -p {q(context)}; "
                f"cp {q(task_dir + '/pyproject.toml')} {q(task_dir + '/uv.lock')} {q(context)}/; "
                + shlex.join(build),
                timeout=_BUILD_TIMEOUT_SECONDS,
            )
            existing = self._ssh(
                f"{docker} image inspect {q(tag)} --format '{{{{.Id}}}}'"
            )
        return ModelEnvImage(tag=tag, image_id=existing.stdout.decode().strip())

    def read_file(self, task_dir: str, path: str) -> str | None:
        result = self._ssh(f"cat {self._q(task_dir + '/' + path)}", check=False)
        if result.returncode != 0:
            return None
        return result.stdout.decode("utf-8")

    def write_files(self, task_dir: str, files: dict[str, str]) -> None:
        if not files:
            return
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode="w") as archive:
            for path, content in sorted(files.items()):
                data = content.encode("utf-8")
                info = tarfile.TarInfo(name=path)
                info.size = len(data)
                info.mode = 0o644
                archive.addfile(info, io.BytesIO(data))
        self._ssh(f"tar -x -C {self._q(task_dir)} -f -", stdin=buffer.getvalue())

    def remove_paths(self, task_dir: str, paths: tuple[str, ...]) -> None:
        if not paths:
            return
        targets = " ".join(self._q(f"{task_dir}/{p}") for p in paths)
        self._ssh(f"rm -f -- {targets}")

    def run_container(
        self, argv: list[str], container_name: str, task_dir: str, wall_seconds: int
    ) -> ModelContainerRunOutcome:
        validate_docker_run_argv(argv, self.task_root())
        killed = False
        try:
            result = self._ssh(shlex.join(argv), timeout=wall_seconds, check=False)
            exit_code: int | None = result.returncode
            stderr_tail = (result.stdout + result.stderr).decode(errors="replace")[
                -1500:
            ]
        except _SshWallClockExceededError:
            killed = True
            exit_code = None
            stderr_tail = f"killed at the wall clock ({wall_seconds}s)"
            self._ssh(
                f"{self._q(self.docker_bin())} kill {self._q(container_name)}",
                check=False,
            )
        junit = self.read_file(task_dir, ".dtl/junit.xml")
        return ModelContainerRunOutcome(
            exit_code=exit_code,
            junit_xml=junit,
            killed_at_wall_clock=killed,
            stderr_tail=stderr_tail,
        )

    def teardown(
        self, repo: str, task_dir: str, correlation_id: str, container_name: str
    ) -> ModelTeardownOutcome:
        q = self._q
        docker = q(self.docker_bin())
        root = self.task_root()
        mirror = f"{root}/mirrors/{_mirror_name(repo)}"
        if not task_dir.startswith(f"{root}/tasks/"):
            raise FocusedTestRunInfraError(
                f"refusing to tear down outside the task root: {task_dir}"
            )
        out = self._ssh(
            f"{docker} rm -f {q(container_name)} >/dev/null 2>&1; "
            f"git -C {q(mirror)} worktree remove --force {q(task_dir)} >/dev/null 2>&1; "
            f'rm -rf {q(task_dir)}; rmdir "$(dirname {q(task_dir)})" 2>/dev/null; '
            f"git -C {q(mirror)} worktree prune >/dev/null 2>&1; "
            f"echo containers=$({docker} ps -aq --filter label={DTL_LABEL_KEY}={q(correlation_id)} | wc -l); "
            f"if [ -e {q(task_dir)} ]; then echo worktree=present; else echo worktree=absent; fi",
            check=False,
        ).stdout.decode()
        return ModelTeardownOutcome(
            container_absent="containers=0" in out.replace(" ", ""),
            worktree_absent="worktree=absent" in out,
        )


__all__ = ["EphemeralContainerFocusedRunSubprocess"]
