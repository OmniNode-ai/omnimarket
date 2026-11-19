# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The overlay roots a node reads through the overlay root resolver.

``ONEX_SKILL_OVERLAY_ROOTS`` names only the roots a host declares, and a host that declares
only the plugin's overlay folder holds no node overlay there. The ordered root list the
resolver builds (the store root, the canonical overlays clone beside the registry root, then
the declared roots) finds the overlay on every host with no per-host edit. ``resolved_overlay_environ``
hands that list to the overlay reader as the one root list it already searches, so a node that
loads its overlay through the reader gets the resolver's order with no reader change.

The registry root is the one the resolver's other caller uses: ``OMNIBASE_PATH``, then ``OMNI_HOME``.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Final

from omnimarket.handlers.node_overlay_reader import OVERLAY_ROOTS_ENV
from omnimarket.models.overlay_roots import ModelOverlayRootsRequest
from omnimarket.nodes.node_overlay_roots_resolve_effect.handlers.handler_overlay_roots_resolve import (
    HandlerOverlayRootsResolve,
)

REGISTRY_ROOT_ENVS: Final[tuple[str, ...]] = ("OMNIBASE_PATH", "OMNI_HOME")


def registry_root_from(environ: Mapping[str, str] | None = None) -> str | None:
    """The registry root the environment names (``OMNIBASE_PATH``, then ``OMNI_HOME``), or ``None``."""
    env = os.environ if environ is None else environ
    for name in REGISTRY_ROOT_ENVS:
        value = env.get(name, "").strip()
        if value:
            return value
    return None


def resolver_overlay_roots(
    environ: Mapping[str, str] | None = None,
) -> tuple[str, ...]:
    """The ordered, present overlay roots: store, canonical overlays clone, then the declared roots."""
    env = os.environ if environ is None else environ
    declared = tuple(
        root for root in env.get(OVERLAY_ROOTS_ENV, "").split(os.pathsep) if root
    )
    request = ModelOverlayRootsRequest(
        declared_roots=declared,
        registry_root=registry_root_from(env),
        home=env.get("HOME") or None,
    )
    return HandlerOverlayRootsResolve().handle(request).paths()


def resolved_overlay_environ(
    environ: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """A copy of the environment whose overlay roots are the resolver's ordered list.

    When the resolver finds no present root the declared value is left as it was, so the
    refusal that follows still names the roots the host declared.
    """
    env = dict(os.environ if environ is None else environ)
    roots = resolver_overlay_roots(env)
    if roots:
        env[OVERLAY_ROOTS_ENV] = os.pathsep.join(roots)
    return env


__all__ = [
    "REGISTRY_ROOT_ENVS",
    "registry_root_from",
    "resolved_overlay_environ",
    "resolver_overlay_roots",
]
