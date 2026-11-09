# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Policy of the agent-prompt gh-read guard: the contract's ``guard_policy``, extended by the overlay (OMN-23307).

Every setting lives in the contract or in the overlay file
``<root>/node_agent_prompt_gh_read_guard_effect/overlay.yaml`` under each root of
``ONEX_SKILL_OVERLAY_ROOTS``. An overlay can add a tool, add a field to a tool or add a code
field; it can never remove one the contract names.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml

from omnimarket.nodes.node_agent_prompt_gh_read_guard_effect.models import (
    ModelAgentPromptGhReadPolicy,
)

CONTRACT = Path(__file__).resolve().parents[1] / "contract.yaml"
OVERLAY_NAME = "overlay.yaml"
NODE_DIR = CONTRACT.parent.name


class GuardPolicyError(Exception):
    """The policy cannot be read; the entrypoint refuses what it cannot judge."""


def _read(path: Path) -> dict[str, Any]:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise GuardPolicyError(f"{path}: {exc}") from exc
    if not isinstance(data, dict):
        raise GuardPolicyError(f"{path}: not a mapping")
    return data


def load_policy(
    environ: dict[str, str] | None = None, contract: Path = CONTRACT
) -> ModelAgentPromptGhReadPolicy:
    env = os.environ if environ is None else environ
    raw = _read(contract).get("guard_policy")
    if not isinstance(raw, dict):
        raise GuardPolicyError(f"{contract}: no guard_policy")
    fields: dict[str, list[str]] = {
        tool: list(names) for tool, names in raw["judged_fields"].items()
    }
    code_fields = list(raw.get("code_fields", ()))
    path_fields = dict(raw.get("path_fields", {}))
    for root in filter(None, env.get("ONEX_SKILL_OVERLAY_ROOTS", "").split(os.pathsep)):
        overlay = Path(root) / NODE_DIR / OVERLAY_NAME
        if not overlay.is_file():
            continue
        extra = _read(overlay)
        for tool, names in (extra.get("judged_fields") or {}).items():
            merged = fields.setdefault(tool, [])
            merged.extend(n for n in names if n not in merged)
        code_fields.extend(
            n for n in extra.get("code_fields") or () if n not in code_fields
        )
        for tool, name in (extra.get("path_fields") or {}).items():
            path_fields.setdefault(tool, name)
    return ModelAgentPromptGhReadPolicy(
        judged_fields={tool: tuple(names) for tool, names in fields.items()},
        code_fields=tuple(code_fields),
        path_fields=path_fields,
        refusal_class=str(raw["refusal_class"]),
    )
