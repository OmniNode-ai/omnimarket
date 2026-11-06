# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The real seams: an http probe of the upstream, the ``tailscale serve`` command, and the ledger append.

Every command is an argument vector run without a shell. The command prefix that gives the tailscale
binary and the root-owned env file their rights (``sudo -n``, or none when the node runs as root) comes
from the overlay.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath

from omnimarket.nodes.node_tailnet_https_serve_effect.models import (
    ModelServeReport,
    ModelServeResult,
    ModelTailscaleBackend,
)
from omnimarket.nodes.node_tailnet_https_serve_effect.protocols import ServeStepError

Runner = Callable[[Sequence[str], int], "subprocess.CompletedProcess[str]"]
EnvRunner = Callable[
    [Sequence[str], int, Mapping[str, str]], "subprocess.CompletedProcess[str]"
]

#: The flag that makes ``tailscale serve`` terminate https on a port. ``--http=`` would serve plain http.
SERVE_PORT_FLAG = "--https="


def run_command(argv: Sequence[str], timeout: int) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            list(argv), capture_output=True, text=True, check=False, timeout=timeout
        )
    except FileNotFoundError as exc:
        return subprocess.CompletedProcess(list(argv), 127, "", str(exc))
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(
            list(argv), 124, "", f"timed out after {timeout}s"
        )


def run_command_with_env(
    argv: Sequence[str], timeout: int, extra_env: Mapping[str, str]
) -> subprocess.CompletedProcess[str]:
    """Run with the overlay's extra environment laid over this process's environment."""
    try:
        return subprocess.run(
            list(argv),
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout,
            env={**os.environ, **extra_env},
        )
    except FileNotFoundError as exc:
        return subprocess.CompletedProcess(list(argv), 127, "", str(exc))
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(
            list(argv), 124, "", f"timed out after {timeout}s"
        )


def _first_line(text: str) -> str:
    for line in text.splitlines():
        if line.strip():
            return line.strip()[:300]
    return "no output"


class HttpUpstreamProbe:
    """True when the upstream answers the path with a status below 400 (redirects are followed)."""

    def answers(self, upstream_url: str, path: str, timeout_seconds: int) -> bool:
        try:
            with urllib.request.urlopen(
                f"{upstream_url}{path}", timeout=timeout_seconds
            ) as response:
                return bool(response.status < 400)
        except (urllib.error.URLError, OSError, ValueError):
            return False


def _site_target(site: object) -> str:
    handlers = site.get("Handlers") if isinstance(site, dict) else None
    parts: list[str] = []
    for path, handler in sorted((handlers or {}).items()):
        proxy = handler.get("Proxy", "") if isinstance(handler, dict) else ""
        if path == "/":
            parts.append(proxy or "non-proxy handler")
        else:
            parts.append(f"{path} -> {proxy or 'non-proxy handler'}")
    return ", ".join(parts)


class TailscaleServeBackend:
    def __init__(self, spec: ModelTailscaleBackend, run: Runner = run_command) -> None:
        self._s = spec
        self._run = run

    def _tailscale(self, *args: str) -> subprocess.CompletedProcess[str]:
        return self._run(
            [*self._s.command_prefix, self._s.binary, *args], self._s.timeout_seconds
        )

    def served_target(self, https_port: int) -> str:
        done = self._tailscale("serve", "status", "--json")
        if done.returncode:
            raise ServeStepError(
                "serve",
                f"serve status exited {done.returncode}: {_first_line(done.stderr)}",
            )
        try:
            document = json.loads(done.stdout) if done.stdout.strip() else {}
        except json.JSONDecodeError as exc:
            raise ServeStepError(
                "serve", f"serve status is not json: {exc.msg}"
            ) from exc
        web = document.get("Web") if isinstance(document, dict) else None
        for hostport, site in sorted((web or {}).items()):
            if hostport.rsplit(":", 1)[-1] == str(https_port):
                return _site_target(site)
        return ""

    def serve(self, https_port: int, upstream_url: str) -> None:
        done = self._tailscale(
            "serve", "--bg", f"{SERVE_PORT_FLAG}{https_port}", upstream_url
        )
        if done.returncode:
            raise ServeStepError(
                "serve",
                f"serve exited {done.returncode}: {_first_line(done.stderr)}",
            )

    def off(self, https_port: int) -> None:
        done = self._tailscale("serve", f"{SERVE_PORT_FLAG}{https_port}", "off")
        if done.returncode:
            raise ServeStepError(
                "serve",
                f"serve off exited {done.returncode}: {_first_line(done.stderr)}",
            )

    def place_env(self, path: str, text: str) -> None:
        directory = str(PurePosixPath(path).parent)
        with tempfile.TemporaryDirectory(prefix="tailnet-serve-env-") as tmp:
            staged = Path(tmp) / "env"
            staged.write_text(text, encoding="utf-8")
            for argv in (
                ["install", "-d", "-m", "0755", directory],
                ["install", "-m", "0644", str(staged), path],
            ):
                done = self._run(
                    [*self._s.command_prefix, *argv], self._s.timeout_seconds
                )
                if done.returncode:
                    raise ServeStepError(
                        "env",
                        f"placing {path} exited {done.returncode}: {_first_line(done.stderr)}",
                    )

    def remove_env(self, path: str) -> None:
        done = self._run(
            [*self._s.command_prefix, "rm", "-f", path], self._s.timeout_seconds
        )
        if done.returncode:
            raise ServeStepError(
                "env",
                f"removing {path} exited {done.returncode}: {_first_line(done.stderr)}",
            )


class AppendReporter:
    """Appends one STATUS row per run through the configured ledger append command."""

    def __init__(
        self, spec: ModelServeReport, run: EnvRunner = run_command_with_env
    ) -> None:
        self._r = spec
        self._run = run

    def row(self, result: ModelServeResult, now: datetime) -> str:
        detail = re.sub(r"\s*\|\s*|\s+", " ", result.detail).strip()[:240]
        return " | ".join(
            [
                now.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "STATUS",
                f"lane={self._r.lane}",
                "src=tailnet-https-serve",
                f"actor={self._r.actor}",
                "model=none",
                f"run={result.run_id}",
                f"mode={result.mode.value}",
                f"outcome={result.outcome.value}",
                f"port={result.https_port}",
                f"changed={str(result.changed).lower()}",
                detail,
            ]
        )

    def report(self, result: ModelServeResult) -> None:
        done = self._run(
            [*self._r.append_argv, self.row(result, datetime.now(UTC))],
            self._r.timeout_seconds,
            self._r.environment,
        )
        if done.returncode:
            raise ServeStepError(
                "report",
                f"the ledger append exited {done.returncode}: {_first_line(done.stderr)}",
            )
