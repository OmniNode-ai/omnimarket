# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerOnexSetup: run the onboarding walk a developer needs after installing the CLI.

Steps, in order (``SETUP_STEPS``):

``workspace``       create the workspace root (OMNIBASE_PATH)
``prerequisites``   choose the install path and check what that path needs
``user_config``     write ``<onex_home>/config.yaml`` when it does not exist
``runtime_config``  write the workspace runtime configuration, then record the
                    workspace and path in ``<onex_home>/setup.json``

Every step is idempotent and every write is atomic (a temporary file renamed
into place), so a walk that stops at a failed step leaves nothing half-written
and the next run resumes from that step. A second run on a configured machine
writes nothing. Which omnibase install.sh step went where is recorded in
``setup_step_inventory.yaml`` beside this node's contract.
"""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import tempfile
from pathlib import Path

from omnibase_core.cli.cli_user_config import USER_CONFIG_TEMPLATE, read_user_config
from omnibase_core.errors.model_onex_error import ModelOnexError
from pydantic import ValidationError

from omnimarket.nodes.node_onex_setup_effect.models import (
    ModelOnexSetupFailure,
    ModelOnexSetupRequest,
    ModelOnexSetupResult,
    ModelOnexSetupState,
    ModelOnexSetupStepOutcome,
)

SETUP_STEPS: tuple[str, ...] = (
    "workspace",
    "prerequisites",
    "user_config",
    "runtime_config",
)
SETUP_MARKER = "# generated-by: onex setup"
INSTALL_SH_MARKER = "# generated-by: omnibase install.sh"
INSTALL_PATHS: tuple[str, ...] = ("local", "docker")
RUNTIME_CONFIG_RELPATH = Path("config/onex/runtime/runtime_config.yaml")
STATE_FILE_NAME = "setup.json"
USER_CONFIG_FILE_NAME = "config.yaml"
_OWNED_MARKERS = (SETUP_MARKER, INSTALL_SH_MARKER)

_RUNTIME_CONFIG_TEMPLATE = """\
{marker}
# install-path: {path}
#
# This workspace's runtime configuration, written for the install path above.
# Rewrite it for another path with:  onex setup --path <local|docker>
# Hand edits are kept only if you delete the first line, after which onex setup
# never touches it.
name: "omnibase-{path}"
description: "{description}"
input_topic: "requests"
output_topic: "responses"
group_id: "onex-runtime"
event_bus:
  type: "{bus_type}"
  profile: "local"
  environment: "local"
  max_history: 1000
  circuit_breaker_threshold: 5
"""

_PATH_SHAPES = {
    "local": (
        "inmemory",
        "onex setup path local: fully local runtime, in-memory event bus, no Docker",
    ),
    "docker": (
        "kafka",
        "onex setup path docker: self-hosted Docker stack, Kafka-compatible event bus",
    ),
}

_DOCKER_TOOLS = {
    "docker": "install Docker (https://docs.docker.com/get-docker/), or use --path local, "
    "which needs no Docker",
    "openssl": "install openssl (macOS: brew install openssl@3)",
    "make": "install make (macOS: xcode-select --install)",
}


def which(name: str) -> str | None:
    """Where ``name`` is on PATH, or None. A seam the tests replace."""
    return shutil.which(name)


def compose_available() -> bool:
    """True when ``docker compose version`` succeeds. A seam the tests replace."""
    try:
        completed = subprocess.run(
            ["docker", "compose", "version"],
            capture_output=True,
            check=False,
        )
    except OSError:
        return False
    return completed.returncode == 0


class _StepFailedError(Exception):
    """One step could not complete; carries its single fix."""

    def __init__(self, step: str, problem: str, fix: str) -> None:
        super().__init__(problem)
        self.step = step
        self.problem = problem
        self.fix = fix


def _write_atomic(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
        tmp.replace(path)
    finally:
        tmp.unlink(missing_ok=True)


def _write_if_changed(path: Path, content: str) -> bool:
    """Write ``content`` unless the file already holds it. True when written."""
    if path.is_file() and path.read_text(encoding="utf-8") == content:
        return False
    _write_atomic(path, content)
    return True


def recorded_state(onex_home: Path) -> ModelOnexSetupState | None:
    """The workspace and path an earlier completed walk recorded, or None."""
    path = onex_home / STATE_FILE_NAME
    if not path.is_file():
        return None
    try:
        return ModelOnexSetupState.model_validate_json(path.read_text(encoding="utf-8"))
    except ValidationError:
        return None


def _recorded_path(runtime_config: Path) -> str | None:
    if not runtime_config.is_file():
        return None
    lines = runtime_config.read_text(encoding="utf-8").splitlines()
    if not lines or lines[0] not in _OWNED_MARKERS:
        return None
    for line in lines[1:4]:
        if line.startswith("# install-path:"):
            value = line.split(":", 1)[1].strip()
            return value if value in INSTALL_PATHS else None
    return None


def retry_command(workspace: Path | None, install_path: str | None) -> str:
    args = ["onex", "setup", "--workspace", str(workspace) if workspace else "DIR"]
    if install_path in INSTALL_PATHS:
        args += ["--path", str(install_path)]
    return shlex.join(args)


def _step_workspace(workspace: Path | None) -> tuple[Path, bool]:
    if workspace is None:
        raise _StepFailedError(
            "workspace",
            "no workspace root is named, and there is no terminal to ask in",
            "pass --workspace DIR, or export OMNIBASE_PATH=DIR, naming the directory "
            "your ONEX workspace lives in",
        )
    root = workspace.expanduser().resolve()
    existed = root.is_dir()
    try:
        root.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise _StepFailedError(
            "workspace",
            f"cannot use {root} as the workspace root ({exc.strerror or exc})",
            f"move aside whatever is at {root}, or pass --workspace with another directory",
        ) from exc
    return root, not existed


def _resolve_path(
    given: str | None, state: ModelOnexSetupState | None, runtime_config: Path
) -> str:
    if given is not None:
        choice = given
    elif state is not None:
        choice = state.install_path
    else:
        choice = _recorded_path(runtime_config) or "local"
    if choice == "cloud":
        raise _StepFailedError(
            "prerequisites",
            "the cloud install path is not available yet",
            "use --path local (no Docker) or --path docker",
        )
    if choice not in INSTALL_PATHS:
        raise _StepFailedError(
            "prerequisites",
            f"unknown install path '{choice}'",
            "use --path local (no Docker) or --path docker",
        )
    return choice


def _step_prerequisites(install_path: str) -> str:
    if install_path == "local":
        return "path local needs nothing beyond this CLI"
    for tool, remedy in _DOCKER_TOOLS.items():
        if which(tool) is None:
            raise _StepFailedError(
                "prerequisites",
                f"the docker install path needs {tool}, which is not on PATH",
                remedy,
            )
        if tool == "docker" and not compose_available():
            raise _StepFailedError(
                "prerequisites",
                "the docker install path needs the docker compose plugin",
                "install the Docker Compose plugin (docker compose version must succeed), "
                "or use --path local",
            )
    return "path docker: docker, docker compose, openssl and make found"


def _step_user_config(onex_home: Path) -> tuple[str, bool]:
    config = onex_home / USER_CONFIG_FILE_NAME
    if config.exists():
        try:
            read_user_config(config)
        except (ModelOnexError, OSError) as exc:
            raise _StepFailedError(
                "user_config",
                f"{config} exists but cannot be read as a config ({exc})",
                f"repair {config}, or move it aside so setup writes a fresh one",
            ) from exc
        return f"{config} kept as is", False
    for sub in ("state", "logs"):
        (onex_home / sub).mkdir(parents=True, exist_ok=True)
    _write_atomic(config, USER_CONFIG_TEMPLATE)
    return str(config), True


def _step_runtime_config(
    workspace: Path, install_path: str, onex_home: Path
) -> tuple[Path, bool]:
    runtime_config = workspace / RUNTIME_CONFIG_RELPATH
    if runtime_config.exists():
        lines = runtime_config.read_text(encoding="utf-8").splitlines()
        if not lines or lines[0] not in _OWNED_MARKERS:
            raise _StepFailedError(
                "runtime_config",
                f"{runtime_config} exists and was not written by onex setup or "
                "install.sh; it is left untouched",
                f"move {runtime_config} aside so setup can write the {install_path} "
                "configuration",
            )
    bus_type, description = _PATH_SHAPES[install_path]
    content = _RUNTIME_CONFIG_TEMPLATE.format(
        marker=SETUP_MARKER,
        path=install_path,
        description=description,
        bus_type=bus_type,
    )
    written = _write_if_changed(runtime_config, content)
    state = ModelOnexSetupState(workspace=workspace, install_path=install_path)
    recorded = _write_if_changed(
        onex_home / STATE_FILE_NAME, state.model_dump_json(indent=2) + "\n"
    )
    return runtime_config, written or recorded


class HandlerOnexSetup:
    """Run every step in order; stop at the first that fails and name its fix."""

    def handle(self, request: ModelOnexSetupRequest) -> ModelOnexSetupResult:
        steps: list[ModelOnexSetupStepOutcome] = []
        workspace: Path | None = request.workspace
        install_path: str | None = request.install_path
        state = recorded_state(request.onex_home)
        try:
            workspace, created = _step_workspace(workspace)
            steps.append(
                ModelOnexSetupStepOutcome(
                    step="workspace", changed=created, detail=str(workspace)
                )
            )
            install_path = _resolve_path(
                request.install_path, state, workspace / RUNTIME_CONFIG_RELPATH
            )
            steps.append(
                ModelOnexSetupStepOutcome(
                    step="prerequisites",
                    changed=False,
                    detail=_step_prerequisites(install_path),
                )
            )
            detail, changed = _step_user_config(request.onex_home)
            steps.append(
                ModelOnexSetupStepOutcome(
                    step="user_config", changed=changed, detail=detail
                )
            )
            runtime_config, changed = _step_runtime_config(
                workspace, install_path, request.onex_home
            )
            steps.append(
                ModelOnexSetupStepOutcome(
                    step="runtime_config", changed=changed, detail=str(runtime_config)
                )
            )
        except _StepFailedError as failed:
            return ModelOnexSetupResult(
                workspace=workspace,
                install_path=install_path,
                steps=tuple(steps),
                failure=ModelOnexSetupFailure(
                    step=failed.step,
                    problem=failed.problem,
                    fix=failed.fix,
                    retry=retry_command(workspace, install_path),
                ),
            )
        return ModelOnexSetupResult(
            workspace=workspace, install_path=install_path, steps=tuple(steps)
        )


__all__ = [
    "INSTALL_PATHS",
    "INSTALL_SH_MARKER",
    "RUNTIME_CONFIG_RELPATH",
    "SETUP_MARKER",
    "SETUP_STEPS",
    "HandlerOnexSetup",
    "compose_available",
    "recorded_state",
    "retry_command",
    "which",
]
