# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The real side effects of an updater tick: git, npm, a candidate instance, a symlink and systemctl.

Every path, address, port and URL comes from the host row; nothing here names a host. The dashboard is
built and served by the dashboard repository's own scripts (``npm ci``, ``generate:config``, ``build``
and ``dev``); this module only places them.
"""

from __future__ import annotations

import contextlib
import os
import platform
import shutil
import signal
import subprocess
import time
import urllib.error
import urllib.request
from collections.abc import Mapping, Sequence
from pathlib import Path
from urllib.parse import urlsplit

import yaml
from omnibase_core.validators.no_unguarded_git_subprocess import (
    scrub_git_location_env,
)

from omnimarket.models.model_omnidash_host import (
    ModelOmnidashBranchTip,
    ModelOmnidashHostRow,
    ModelOmnidashHostSchedule,
)

from .handler_omnidash_host_effect import OmnidashPollError

BUILD_OK_MARKER = ".omnidash-host-build-ok"
# Vite 6 answers only IP and localhost Host headers unless the serving name is added here.
ALLOWED_HOSTS_ENV = "__VITE_ADDITIONAL_SERVER_ALLOWED_HOSTS"


def serve_hostname(host: ModelOmnidashHostRow) -> str:
    name = urlsplit(host.serve_url).hostname
    if not name:
        raise ValueError("serve_url names no host")
    return name


def node_archive(row: ModelOmnidashHostRow) -> str:
    """The directory name the pinned node distribution unpacks to."""
    machine = platform.machine()
    arch = {"x86_64": "x64", "aarch64": "arm64"}.get(machine)
    if arch is None:
        raise ValueError(f"no node distribution for machine {machine!r}")
    return f"node-v{row.node_version}-linux-{arch}"


def node_bin(host: ModelOmnidashHostRow) -> Path:
    """The pinned node's bin directory under the tools root (user space)."""
    return Path(host.tools_root) / node_archive(host) / "bin"


def service_env(host: ModelOmnidashHostRow) -> dict[str, str]:
    base = os.environ
    return {
        "HOME": base.get("HOME", ""),
        "PATH": f"{node_bin(host)}:/usr/local/bin:/usr/bin:/bin",
        ALLOWED_HOSTS_ENV: serve_hostname(host),
        "CI": "1",
    }


def _run(
    argv: Sequence[str], *, cwd: Path | None, env: Mapping[str, str], timeout: int
) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            list(argv),
            cwd=cwd,
            env=scrub_git_location_env(env),
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout,
        )
    except FileNotFoundError as exc:
        return subprocess.CompletedProcess(list(argv), 127, "", str(exc))
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(
            list(argv), 124, "", f"{argv[0]} exceeded {timeout}s"
        )


def _tail(done: subprocess.CompletedProcess[str]) -> str:
    text = (done.stderr or done.stdout or "").strip().splitlines()
    return " | ".join(text[-6:])[:600]


def http_status(url: str, host_header: str | None, timeout: float) -> int:
    request = urllib.request.Request(url)
    if host_header:
        request.add_header("Host", host_header)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return int(response.status)
    except urllib.error.HTTPError as exc:
        return int(exc.code)
    except (urllib.error.URLError, OSError):
        return 0


class SystemOmnidashHostPorts:
    """Real ports for one host row."""

    def __init__(self, *, sleep: float = 1.0) -> None:
        self._sleep = sleep

    # -- paths ---------------------------------------------------------------------------------
    @staticmethod
    def builds_dir(host: ModelOmnidashHostRow) -> Path:
        return Path(host.build_root) / "builds"

    @staticmethod
    def mirror(host: ModelOmnidashHostRow, schedule: ModelOmnidashHostSchedule) -> Path:
        return Path(host.build_root) / schedule.mirror_directory

    # -- origin --------------------------------------------------------------------------------
    def list_branches(
        self, host: ModelOmnidashHostRow, schedule: ModelOmnidashHostSchedule
    ) -> list[ModelOmnidashBranchTip]:
        mirror = self.mirror(host, schedule)
        env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
        if not mirror.is_dir():
            mirror.parent.mkdir(parents=True, exist_ok=True)
            made = _run(
                ["git", "init", "--quiet", "--bare", str(mirror)],
                cwd=None,
                env=env,
                timeout=60,
            )
            if made.returncode:
                raise OmnidashPollError(f"git init failed: {_tail(made)}")
        fetched = _run(
            [
                "git",
                "-C",
                str(mirror),
                "fetch",
                "--quiet",
                "--prune",
                "--no-tags",
                host.repo_url,
                "+refs/heads/*:refs/heads/*",
            ],
            cwd=None,
            env=env,
            timeout=schedule.build_timeout_seconds,
        )
        if fetched.returncode:
            raise OmnidashPollError(f"fetch failed: {_tail(fetched)}")
        return self._tips(mirror, env)

    @staticmethod
    def _tips(mirror: Path, env: Mapping[str, str]) -> list[ModelOmnidashBranchTip]:
        listed = _run(
            [
                "git",
                "-C",
                str(mirror),
                "for-each-ref",
                "--sort=-committerdate",
                "--format=%(objectname) %(committerdate:unix) %(refname:short)",
                "refs/heads",
            ],
            cwd=None,
            env=env,
            timeout=60,
        )
        if listed.returncode:
            raise OmnidashPollError(f"for-each-ref failed: {_tail(listed)}")
        tips = []
        for line in listed.stdout.splitlines():
            sha, stamp, branch = line.split(" ", 2)
            tips.append(
                ModelOmnidashBranchTip(branch=branch, sha=sha, committed_at=int(stamp))
            )
        return tips

    def resolve_commit(
        self,
        host: ModelOmnidashHostRow,
        schedule: ModelOmnidashHostSchedule,
        sha: str,
    ) -> ModelOmnidashBranchTip | None:
        mirror = self.mirror(host, schedule)
        env = dict(os.environ)
        shown = _run(
            ["git", "-C", str(mirror), "show", "-s", "--format=%H %ct", sha],
            cwd=None,
            env=env,
            timeout=60,
        )
        if shown.returncode:
            return None
        full, stamp = shown.stdout.split()
        holders = _run(
            [
                "git",
                "-C",
                str(mirror),
                "branch",
                "--contains",
                full,
                "--format=%(refname:short)",
            ],
            cwd=None,
            env=env,
            timeout=60,
        )
        names = holders.stdout.split()
        return ModelOmnidashBranchTip(
            branch=names[0] if names else "pinned", sha=full, committed_at=int(stamp)
        )

    # -- build ---------------------------------------------------------------------------------
    def build(
        self,
        host: ModelOmnidashHostRow,
        schedule: ModelOmnidashHostSchedule,
        sha: str,
    ) -> str | None:
        target = self.builds_dir(host) / sha
        if (target / BUILD_OK_MARKER).is_file():
            return None
        shutil.rmtree(target, ignore_errors=True)
        target.parent.mkdir(parents=True, exist_ok=True)
        env = {**os.environ, **service_env(host)}
        mirror = self.mirror(host, schedule)
        steps: list[tuple[str, list[str], Path | None]] = [
            (
                "clone",
                ["git", "clone", "--quiet", "--no-checkout", str(mirror), str(target)],
                None,
            ),
            (
                "checkout",
                ["git", "-C", str(target), "checkout", "--quiet", "--detach", sha],
                None,
            ),
        ]
        for name, argv, cwd in steps:
            done = _run(argv, cwd=cwd, env=env, timeout=schedule.build_timeout_seconds)
            if done.returncode:
                shutil.rmtree(target, ignore_errors=True)
                return f"{name} failed (exit {done.returncode}): {_tail(done)}"
        (target / "contract.local.yaml").write_text(
            yaml.safe_dump(
                {
                    "data_source": {
                        "url": host.projection_api_url,
                        "projection_tenant_id": host.projection_tenant_id,
                    }
                },
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        for name, argv in (
            ("npm ci", ["npm", "ci", "--no-audit", "--no-fund"]),
            ("generate:config", ["npm", "run", "generate:config"]),
            ("build", ["npm", "run", "build"]),
        ):
            done = _run(
                argv, cwd=target, env=env, timeout=schedule.build_timeout_seconds
            )
            if done.returncode:
                reason = f"{name} failed (exit {done.returncode}): {_tail(done)}"
                shutil.rmtree(target, ignore_errors=True)
                return reason
        (target / BUILD_OK_MARKER).write_text(sha + "\n", encoding="utf-8")
        return None

    # -- health --------------------------------------------------------------------------------
    def check_health(
        self,
        host: ModelOmnidashHostRow,
        schedule: ModelOmnidashHostSchedule,
        sha: str,
    ) -> str | None:
        target = self.builds_dir(host) / sha
        env = {**os.environ, **service_env(host)}
        serve = urlsplit(host.serve_url)
        header = f"{serve.hostname}:{host.scratch_port}"
        process = subprocess.Popen(
            [
                "npm",
                "run",
                "dev",
                "--",
                "--host",
                host.bind_address,
                "--port",
                str(host.scratch_port),
                "--strictPort",
            ],
            cwd=target,
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        try:
            return self._wait_ok(
                f"http://{host.bind_address}:{host.scratch_port}{schedule.health_path}",
                header,
                schedule.health_timeout_seconds,
                process,
            )
        finally:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGTERM)
            with contextlib.suppress(subprocess.TimeoutExpired):
                process.wait(timeout=15)
            if process.poll() is None:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(process.pid, signal.SIGKILL)

    def _wait_ok(
        self,
        url: str,
        header: str,
        timeout_s: int,
        process: subprocess.Popen[bytes] | None,
    ) -> str | None:
        deadline = time.monotonic() + timeout_s
        code = 0
        while time.monotonic() < deadline:
            if process is not None and process.poll() is not None:
                return f"candidate exited with {process.returncode} before answering"
            code = http_status(url, header, 5.0)
            if code == 200:
                return None
            time.sleep(self._sleep)
        return f"health check {urlsplit(url).path} answered {code} within {timeout_s}s"

    # -- switch --------------------------------------------------------------------------------
    def switch(
        self,
        host: ModelOmnidashHostRow,
        schedule: ModelOmnidashHostSchedule,
        sha: str,
    ) -> str | None:
        root = Path(host.build_root)
        link = root / schedule.current_link
        previous = os.readlink(link) if link.is_symlink() else None
        self._point(link, str(self.builds_dir(host) / sha))
        failure = self._restart_and_probe(host, schedule)
        if failure is None:
            return None
        if previous is not None:
            self._point(link, previous)
            self._restart_and_probe(host, schedule)
        else:
            link.unlink(missing_ok=True)
        return f"switch: {failure}"

    @staticmethod
    def _point(link: Path, target: str) -> None:
        scratch = link.with_name(f".{link.name}.{os.getpid()}.new")
        scratch.unlink(missing_ok=True)
        scratch.symlink_to(target)
        scratch.replace(link)

    def _restart_and_probe(
        self, host: ModelOmnidashHostRow, schedule: ModelOmnidashHostSchedule
    ) -> str | None:
        env = dict(os.environ)
        done = _run(
            ["systemctl", "--user", "restart", f"{schedule.service_unit}.service"],
            cwd=None,
            env=env,
            timeout=schedule.health_timeout_seconds,
        )
        if done.returncode:
            return f"systemctl restart failed (exit {done.returncode}): {_tail(done)}"
        serve = urlsplit(host.serve_url)
        return self._wait_ok(
            f"http://{host.bind_address}:{host.port}{schedule.health_path}",
            f"{serve.hostname}:{host.port}",
            schedule.health_timeout_seconds,
            None,
        )

    # -- retention -----------------------------------------------------------------------------
    def prune(
        self,
        host: ModelOmnidashHostRow,
        schedule: ModelOmnidashHostSchedule,
        keep: frozenset[str],
    ) -> None:
        builds = self.builds_dir(host)
        if not builds.is_dir():
            return
        others = sorted(
            (child for child in builds.iterdir() if child.name not in keep),
            key=lambda child: child.stat().st_mtime,
            reverse=True,
        )
        for stale in others[schedule.keep_builds :]:
            shutil.rmtree(stale, ignore_errors=True)
