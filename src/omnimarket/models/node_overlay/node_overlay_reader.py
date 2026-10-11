# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Locate and read a node's deployment overlay.

A node's deployment facts (hosts, endpoints, routing order) are supplied by
whoever runs the system, never shipped with the package. The overlay for node
``<name>`` is the file ``<name>/overlay.yaml`` under the first of the
directories named by ``ONEX_SKILL_OVERLAY_ROOTS`` (an ``os.pathsep`` separated
list) that holds it. A node may also accept an explicit pointer variable, which
wins and never falls through to the roots.

No overlay is not an error here: the caller decides between a neutral default
and a loud refusal, using :func:`overlay_hint` to tell the operator how to
supply one.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml

ROOTS_ENV = "ONEX_SKILL_OVERLAY_ROOTS"
OVERLAY_FILENAME = "overlay.yaml"


class NodeOverlayError(ValueError):
    """An overlay was named but cannot be read as a mapping."""


def overlay_hint(node_name: str, pointer_env: str | None = None) -> str:
    """Return the sentence that tells an operator how to supply ``node_name``'s overlay."""
    pointer = f"{pointer_env} (a file) or " if pointer_env else ""
    return (
        f"supply the {node_name} overlay through {pointer}{ROOTS_ENV} "
        f"(a root directory holding {node_name}/{OVERLAY_FILENAME})"
    )


def find_node_overlay(node_name: str, pointer_env: str | None = None) -> Path | None:
    """Return the overlay path for ``node_name``, or ``None`` when none is supplied."""
    if pointer_env is not None and pointer_env in os.environ:
        pointer = os.environ[pointer_env]
        if not pointer:
            raise NodeOverlayError(f"{pointer_env}: empty overlay pointer")
        return Path(pointer)
    for root in os.environ.get(ROOTS_ENV, "").split(os.pathsep):
        if not root:
            continue
        candidate = Path(root) / node_name / OVERLAY_FILENAME
        if candidate.exists():
            return candidate
    return None


def load_node_overlay(
    node_name: str, pointer_env: str | None = None
) -> dict[str, Any] | None:
    """Read ``node_name``'s overlay mapping, or ``None`` when none is supplied.

    The error names the source, never the content.
    """
    path = find_node_overlay(node_name, pointer_env)
    if path is None:
        return None
    source = pointer_env if pointer_env and pointer_env in os.environ else ROOTS_ENV
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise NodeOverlayError(
            f"{source}: unreadable overlay for {node_name} ({type(exc).__name__})"
        ) from None
    if not isinstance(raw, dict):
        raise NodeOverlayError(f"{source}: overlay for {node_name} must be a mapping")
    return raw


def require_overlay_keys(
    node_name: str, raw: dict[str, Any], allowed: frozenset[str]
) -> None:
    """Refuse keys outside ``allowed`` so a typo cannot silently route elsewhere."""
    unknown = sorted(set(raw) - allowed)
    if unknown:
        raise NodeOverlayError(
            f"overlay for {node_name} carries unknown keys: {', '.join(unknown)}"
        )


__all__ = [
    "OVERLAY_FILENAME",
    "ROOTS_ENV",
    "NodeOverlayError",
    "find_node_overlay",
    "load_node_overlay",
    "overlay_hint",
    "require_overlay_keys",
]
