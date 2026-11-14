# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Pure argv, environment, path and parse helpers for the compose effect.

No I/O beyond ``Path`` construction and the reads ``digest_files`` performs;
every host, binary, project and filter comes from the overlays or the stack
description as data.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import TYPE_CHECKING

from omnimarket.nodes.node_stability_stack_compose_effect.overlay import (
    ModelStackComposeOverlay,
)
from omnimarket.nodes.node_stability_stack_plan_compute.models.model_stack_plan import (
    ModelStackDescription,
)

if TYPE_CHECKING:
    from collections.abc import Mapping

__all__ = [
    "compose_argv",
    "compose_env",
    "consume_argv",
    "digest_files",
    "gone_argvs",
    "parse_ps",
    "produce_argv",
    "rendered_paths",
]


def compose_env(
    environ: Mapping[str, str], stack: ModelStackDescription
) -> dict[str, str]:
    """The environment compose runs with: the ambient env plus the stack's pairs."""
    env = dict(environ)
    env.update(stack.env)
    env.update({key: str(port) for key, port in stack.ports})
    return env


def rendered_paths(
    overlay: ModelStackComposeOverlay,
    stack: ModelStackDescription,
    environ: Mapping[str, str],
) -> tuple[Path, ...]:
    """Where each rendered compose file lives; ``KeyError`` when the dir is unset."""
    render_dir = environ[overlay.render_dir_env]
    base = Path(render_dir) / stack.compose_project
    return tuple(base / Path(name).name for name in stack.compose_files)


def compose_argv(
    overlay: ModelStackComposeOverlay,
    stack: ModelStackDescription,
    files: tuple[Path, ...],
    *verb: str,
) -> tuple[str, ...]:
    """The compose argv: binary, compose, project, one -f per file, then the verb."""
    argv: list[str] = [overlay.docker_binary, "compose", "-p", stack.compose_project]
    for path in files:
        argv.extend(("-f", str(path)))
    argv.extend(verb)
    return tuple(argv)


def parse_ps(stdout: str) -> tuple[dict[str, object], ...]:
    """Parse ``ps --format json`` output: json lines or one json array."""
    stripped = stdout.strip()
    if not stripped:
        return ()
    rows: list[dict[str, object]] = []
    if stripped.startswith("["):
        loaded = json.loads(stripped)
        if isinstance(loaded, list):
            rows.extend(item for item in loaded if isinstance(item, dict))
        return tuple(rows)
    for line in stripped.splitlines():
        line = line.strip()
        if not line:
            continue
        item = json.loads(line)
        if isinstance(item, dict):
            rows.append(item)
    return tuple(rows)


def produce_argv(
    overlay: ModelStackComposeOverlay,
    stack: ModelStackDescription,
    message: str,
) -> tuple[str, ...]:
    """The argv that publishes one message on the stack's broker."""
    roundtrip = stack.event_roundtrip
    shell = f"printf %s '{message}' | rpk topic produce {roundtrip.topic}"
    return (
        overlay.docker_binary,
        "exec",
        roundtrip.broker_container,
        "sh",
        "-c",
        shell,
    )


def consume_argv(
    overlay: ModelStackComposeOverlay,
    stack: ModelStackDescription,
) -> tuple[str, ...]:
    """The argv that reads one message back off the stack's broker topic."""
    roundtrip = stack.event_roundtrip
    return (
        overlay.docker_binary,
        "exec",
        roundtrip.broker_container,
        "rpk",
        "topic",
        "consume",
        roundtrip.topic,
        "-o",
        "start",
        "-n",
        "1",
        "--format",
        "%v",
    )


def gone_argvs(
    overlay: ModelStackComposeOverlay,
    stack: ModelStackDescription,
) -> tuple[tuple[str, ...], ...]:
    """The leftover-container and (when volumes go too) leftover-volume listings."""
    label_filter = (
        "--filter",
        f"label=com.docker.compose.project={stack.compose_project}",
    )
    ps_argv = (overlay.docker_binary, "ps", "-a", "-q", *label_filter)
    if stack.remove_volumes_on_down:
        volume_argv = (overlay.docker_binary, "volume", "ls", "-q", *label_filter)
        return (ps_argv, volume_argv)
    return (ps_argv,)


def digest_files(files: tuple[Path, ...]) -> str:
    """The sha256 hex of the file texts joined in order."""
    joined = "".join(path.read_text(encoding="utf-8") for path in files)
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()
