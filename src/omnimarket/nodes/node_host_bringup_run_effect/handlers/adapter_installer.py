# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Backends of the bring-up ports: the in-memory default and the real host installer."""

from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from pydantic import BaseModel

from omnimarket.nodes.node_host_bringup_run_effect.models import ModelInstallerOutcome

_NO_FLAG_MODE = "install"
_TIMEOUT_EXIT = 124
_NOT_FOUND_EXIT = 127


@dataclass
class InMemoryInstaller:
    """Records every call and answers 0, or the exit code ``fail`` names for a unit."""

    fail: dict[str, int] = field(default_factory=dict)
    calls: list[tuple[str, str, str | None]] = field(default_factory=list)

    def run(self, unit: str, mode: str, host_name: str | None) -> ModelInstallerOutcome:
        self.calls.append((unit, mode, host_name))
        code = self.fail.get(unit, 0)
        return ModelInstallerOutcome(
            exit_code=code,
            stdout=f"{unit} {mode} ok" if code == 0 else "",
            stderr="" if code == 0 else f"{unit} {mode} failed",
        )


@dataclass(frozen=True)
class InMemoryDeployStore:
    hosts: frozenset[str] = frozenset()

    def has_deploy_agent_store(self, host_name: str) -> bool:
        return host_name in self.hosts


@dataclass(frozen=True)
class CliHostInstaller:
    """Runs the declared installer command: ``<command> <unit> --repo-root <root> [--mode]``.

    The command, the clone and the timeout come from the node overlay. Install is the
    installer's default mode and carries no flag.
    """

    command: tuple[str, ...]
    repo_root: str
    timeout_seconds: int

    def argv(self, unit: str, mode: str, host_name: str | None) -> list[str]:
        argv = [*self.command, unit, "--repo-root", self.repo_root]
        if mode != _NO_FLAG_MODE:
            argv.append(f"--{mode}")
        if host_name is not None:
            argv.extend(["--host-name", host_name])
        return argv

    def run(self, unit: str, mode: str, host_name: str | None) -> ModelInstallerOutcome:
        try:
            done = subprocess.run(
                self.argv(unit, mode, host_name),
                capture_output=True,
                text=True,
                timeout=self.timeout_seconds,
                check=False,
            )
        except FileNotFoundError as exc:
            return ModelInstallerOutcome(
                exit_code=_NOT_FOUND_EXIT,
                stderr=f"installer command not found: {exc.filename}",
            )
        except subprocess.TimeoutExpired:
            return ModelInstallerOutcome(
                exit_code=_TIMEOUT_EXIT,
                stderr=f"{unit} {mode} exceeded {self.timeout_seconds} s",
            )
        return ModelInstallerOutcome(
            exit_code=done.returncode, stdout=done.stdout, stderr=done.stderr
        )


@dataclass(frozen=True)
class FileDeployStore:
    """Reads the workspace config root document's ``hosts.<name>.deploy_agent_env_stores``."""

    path: Path | None

    def has_deploy_agent_store(self, host_name: str) -> bool:
        if self.path is None or not self.path.is_file():
            return False
        document = yaml.safe_load(self.path.read_text(encoding="utf-8"))
        hosts = document.get("hosts") if isinstance(document, dict) else None
        entry = hosts.get(host_name) if isinstance(hosts, dict) else None
        stores = (
            entry.get("deploy_agent_env_stores") if isinstance(entry, dict) else None
        )
        return bool(stores)


@dataclass
class InMemoryPublisher:
    """Records every published (topic, event); the in-memory default."""

    events: list[tuple[str, BaseModel]] = field(default_factory=list)

    def publish(self, topic: str, event: BaseModel) -> None:
        self.events.append((topic, event))


class StreamEventPublisher:
    """Emits the topic-addressed event as one JSON line on stderr; stdout stays the report.

    No Kafka producer is wired here: a host run with no runtime bus writes the same
    topic-addressed event a runtime would carry.
    """

    def publish(self, topic: str, event: BaseModel) -> None:
        sys.stderr.write(
            json.dumps({"topic": topic, "event": event.model_dump(mode="json")}) + "\n"
        )
