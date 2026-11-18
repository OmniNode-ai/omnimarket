# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The goal-scope overlays, read store-first (OMN-23851).

The goal's ticket rule (``goal_scope_ticket_rule``, OMN-22570) and the ticket scope (``ticket_scope``,
OMN-23184) are deployment facts: which tickets are the goal's roots and which Linear projects are in
scope change with every ruling. Each document is read from the overlay store root first
(``<root>/<overlay id>/overlay.yaml`` under the first root in ``ONEX_SKILL_OVERLAY_ROOTS`` that holds
one) and only when the store holds none from the installed onex-overlays plugin path, as the delegation
routing overlay is (OMN-23826). A document that is present and broken refuses with the key it was
found under and its path; a broken store document never falls through to the plugin path. A document
found in neither place is ``None``: the rule is off.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import yaml

from omnimarket.handlers.node_overlay_reader import OVERLAY_ROOTS_ENV, find_node_overlay
from omnimarket.models.goal_scope_admission import (
    ModelGoalTicketRule,
    ModelTicketScope,
)

GOAL_RULE_OVERLAY_ID = "goal_scope_ticket_rule"
TICKET_SCOPE_OVERLAY_ID = "ticket_scope"
PLUGIN_OVERLAY_SOURCE = "the onex-overlays plugin path"
OVERLAY_PLUGIN_NAME = "onex-overlays"

_COMMON_SCALARS = frozenset(
    {
        "overlay_version",
        "ticket",
        "ruling",
        "endpoint",
        "cache_seconds",
        "reads_per_tick",
        "read_timeout_seconds",
    }
)
_RULE_KEYS = _COMMON_SCALARS | {"max_depth", "ticket_roots"}
_SCOPE_KEYS = _COMMON_SCALARS | {"project_ids", "project_names"}
_RULE_INTS = ("cache_seconds", "reads_per_tick", "read_timeout_seconds", "max_depth")
_SCOPE_INTS = ("cache_seconds", "reads_per_tick", "read_timeout_seconds")
_TICKET = re.compile(r"OMN-\d+")


class GoalScopeOverlayError(ValueError):
    """A goal-scope overlay document is present and unusable; the message names its key and path."""


def installed_plugin_overlay_roots(environ: Mapping[str, str]) -> list[Path]:
    """Where the onex-overlays plugin keeps its ``overlays/`` on this host: the installed plugin's, then the marketplace clones'.

    The config dir is ``CLAUDE_CONFIG_DIR``, else the one the running plugin lives in
    (``<config>/plugins/cache/<marketplace>/<plugin>/<version>``), else ``~/.claude``; an environment
    with none of them yields nothing.
    """
    config = environ.get("CLAUDE_CONFIG_DIR")
    plugin_root = Path(environ.get("CLAUDE_PLUGIN_ROOT") or "/")
    if (
        not config
        and len(plugin_root.parents) > 4
        and plugin_root.parents[2].name == "cache"
    ):
        config = str(plugin_root.parents[4])
    if not config and environ.get("HOME"):
        config = str(Path(environ["HOME"]) / ".claude")
    if not config:
        return []
    plugins = Path(config) / "plugins"
    roots: list[Path] = []
    try:
        entries = json.loads(
            (plugins / "installed_plugins.json").read_text(encoding="utf-8")
        ).get("plugins", {})
    except (OSError, ValueError, AttributeError):
        entries = {}
    for key, installs in sorted(entries.items()):
        if key.split("@", 1)[0] == OVERLAY_PLUGIN_NAME and isinstance(installs, list):
            roots += [
                Path(i["installPath"]) / "overlays"
                for i in installs
                if isinstance(i, dict) and i.get("installPath")
            ]
    roots += sorted(
        plugins.glob(f"marketplaces/*/plugins/{OVERLAY_PLUGIN_NAME}/overlays")
    )
    return roots


def find_goal_overlay(
    overlay_id: str, environ: Mapping[str, str] | None = None
) -> tuple[Path, str] | None:
    """``(path, key)`` of the overlay document, store first, or ``None`` when neither source holds it.

    ``key`` is ``ONEX_SKILL_OVERLAY_ROOTS`` for a store document and ``PLUGIN_OVERLAY_SOURCE`` for one
    found on the plugin path, which is looked at only when the store holds none.
    """
    env = os.environ if environ is None else environ
    store = find_node_overlay(overlay_id, env)
    if store is not None:
        return store, OVERLAY_ROOTS_ENV
    for root in installed_plugin_overlay_roots(env):
        candidate = root / overlay_id / "overlay.yaml"
        if candidate.is_file():
            return candidate, PLUGIN_OVERLAY_SOURCE
    return None


def _read_document(
    path: Path, key: str, keys: frozenset[str]
) -> tuple[dict[str, Any], str]:
    """The overlay mapping and its error prefix; refuses a document that is unreadable, not a mapping, or short of or past ``keys``."""
    where = f"{key}: overlay {path}"
    try:
        doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise GoalScopeOverlayError(
            f"{where} is unreadable ({type(exc).__name__})"
        ) from None
    if not isinstance(doc, dict):
        raise GoalScopeOverlayError(f"{where} must be a mapping")
    missing, unknown = sorted(keys - set(doc)), sorted(set(doc) - keys)
    if missing or unknown:
        why = "; ".join(
            part
            for part in (
                f"lacks {', '.join(missing)}" if missing else "",
                f"carries unknown {', '.join(unknown)}" if unknown else "",
            )
            if part
        )
        raise GoalScopeOverlayError(f"{where} {why}")
    return doc, where


def _whole_numbers(
    doc: Mapping[str, Any], names: tuple[str, ...], where: str
) -> dict[str, int]:
    out: dict[str, int] = {}
    for name in names:
        value = doc[name]
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise GoalScopeOverlayError(
                f"{where}: {name} must be a whole number of at least 1"
            )
        out[name] = value
    return out


def _endpoint(doc: Mapping[str, Any], where: str) -> str:
    if not isinstance(doc["endpoint"], str) or not doc["endpoint"].strip():
        raise GoalScopeOverlayError(f"{where}: endpoint must be one URL")
    return doc["endpoint"]


def load_goal_ticket_rule(
    environ: Mapping[str, str] | None = None,
) -> ModelGoalTicketRule | None:
    """The goal's ticket rule, store first; ``None`` when no ``goal_scope_ticket_rule`` document is installed.

    Raises :class:`GoalScopeOverlayError` naming the key and path when a document is present and wrong.
    """
    found = find_goal_overlay(GOAL_RULE_OVERLAY_ID, environ)
    if found is None:
        return None
    path, key = found
    doc, where = _read_document(path, key, _RULE_KEYS)
    roots = doc["ticket_roots"]
    if (
        not isinstance(roots, list)
        or not roots
        or not all(isinstance(r, str) and _TICKET.fullmatch(r) for r in roots)
    ):
        raise GoalScopeOverlayError(
            f"{where}: ticket_roots must list at least one OMN-<n> ticket"
        )
    numbers = _whole_numbers(doc, _RULE_INTS, where)
    return ModelGoalTicketRule(
        roots=frozenset(roots), endpoint=_endpoint(doc, where), **numbers
    )


def load_ticket_scope(
    environ: Mapping[str, str] | None = None,
) -> ModelTicketScope | None:
    """The shared ticket scope, store first; ``None`` when no ``ticket_scope`` document is installed.

    Raises :class:`GoalScopeOverlayError` naming the key and path when a document is present and wrong.
    """
    found = find_goal_overlay(TICKET_SCOPE_OVERLAY_ID, environ)
    if found is None:
        return None
    path, key = found
    doc, where = _read_document(path, key, _SCOPE_KEYS)
    lists = {name: doc[name] for name in ("project_ids", "project_names")}
    if not all(
        isinstance(v, list) and all(isinstance(i, str) for i in v)
        for v in lists.values()
    ) or not any(lists.values()):
        raise GoalScopeOverlayError(
            f"{where}: project_ids and project_names must be lists and name at least one project between them"
        )
    numbers = _whole_numbers(doc, _SCOPE_INTS, where)
    return ModelTicketScope(
        project_ids=frozenset(lists["project_ids"]),
        project_names=frozenset(lists["project_names"]),
        endpoint=_endpoint(doc, where),
        **numbers,
    )


__all__ = [
    "GOAL_RULE_OVERLAY_ID",
    "PLUGIN_OVERLAY_SOURCE",
    "TICKET_SCOPE_OVERLAY_ID",
    "GoalScopeOverlayError",
    "find_goal_overlay",
    "installed_plugin_overlay_roots",
    "load_goal_ticket_rule",
    "load_ticket_scope",
]
