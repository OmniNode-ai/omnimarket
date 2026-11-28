# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The real container runtime of the customer-path test: the docker command line, one call per operation."""

from __future__ import annotations

import subprocess
import uuid

from omnimarket.nodes.node_customer_path_container_test_effect.models.model_customer_path_container_test import (
    ModelExecResult,
)


class DockerContainerRuntime:
    """Starts a throwaway container with NET_ADMIN (root only, to install routes) and a public resolver, then execs into it."""

    def __init__(self, dns: str, docker: str = "docker") -> None:
        self._dns = dns
        self._docker = docker

    def _run(
        self, args: list[str], *, stdin: str | None, timeout_s: float
    ) -> ModelExecResult:
        try:
            done = subprocess.run(
                [self._docker, *args],
                input=stdin,
                capture_output=True,
                text=True,
                timeout=timeout_s,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return ModelExecResult(exit_code=124, stdout="", stderr="timed out")
        return ModelExecResult(
            exit_code=done.returncode, stdout=done.stdout, stderr=done.stderr
        )

    def start(self, image: str, ttl_s: int) -> str:
        name = f"customer-path-{uuid.uuid4().hex[:12]}"
        done = self._run(
            [
                "run", "-d", "--name", name, "--cap-add", "NET_ADMIN",
                "--dns", self._dns, "--memory", "3g", image, "sleep", str(ttl_s),
            ],
            stdin=None,
            timeout_s=600,
        )  # fmt: skip
        if done.exit_code != 0:
            raise RuntimeError(f"docker run failed: {done.stderr.strip()[-300:]}")
        return name

    def exec(
        self,
        container: str,
        argv: list[str],
        *,
        user: str,
        stdin: str | None,
        timeout_s: float,
    ) -> ModelExecResult:
        args = ["exec", "-u", user]
        if user != "root":
            args += ["-w", f"/home/{user}"]
        if stdin is not None:
            args.append("-i")
        return self._run([*args, container, *argv], stdin=stdin, timeout_s=timeout_s)

    def copy_in(self, container: str, source: str, destination: str) -> ModelExecResult:
        return self._run(
            ["cp", f"{source}/.", f"{container}:{destination}"],
            stdin=None,
            timeout_s=300,
        )

    def remove(self, container: str) -> None:
        self._run(["rm", "-f", container], stdin=None, timeout_s=60)
