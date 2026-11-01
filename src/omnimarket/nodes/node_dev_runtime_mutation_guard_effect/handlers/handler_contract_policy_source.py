# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Reads the guard policy from the node contract and its private overlay (OMN-21795).

The contract's ``guard_policy`` block is the public declaration: the dev runtime
compose project, its compose files and container names, the mutating verbs and the
grant row shape. An overlay can only add to it. The node's deployment overlay,
``<root>/node_dev_runtime_mutation_guard_effect/overlay.yaml`` under
``ONEX_SKILL_OVERLAY_ROOTS`` (found by the shared node overlay reader), can add
protected projects, compose files, container names and verbs. It can never
remove one, so an overlay cannot weaken the guard. A malformed contract or overlay raises an error, and the hook refuses
the call, because a guard with no policy cannot decide.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path

import yaml

from omnimarket.handlers.node_overlay_reader import find_node_overlay
from omnimarket.nodes.node_dev_runtime_mutation_guard_effect.models import (
    ModelDevRuntimeGuardPolicy,
)

CONTRACT_PATH = Path(__file__).resolve().parents[1] / "contract.yaml"
NODE_NAME = "node_dev_runtime_mutation_guard_effect"


class GuardPolicyError(ValueError):
    """The contract or an overlay does not parse into a guard policy."""


def _load_mapping(path: Path) -> dict[str, object]:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise GuardPolicyError(f"cannot read {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise GuardPolicyError(f"{path} is not a mapping")
    return data


def _strings(value: object, where: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise GuardPolicyError(f"{where} must be a list of strings")
    return list(value)


def _merge_unique(base: list[str], extra: list[str]) -> list[str]:
    return base + [v for v in extra if v not in base]


def merge_overlay(
    base: dict[str, object], overlay: Mapping[str, object], where: str
) -> dict[str, object]:
    """``base`` with the overlay's projects, files, containers and verbs added."""
    allowed = {"protected_projects", "compose_mutating_verbs", "docker_mutating_verbs"}
    unknown = sorted(set(overlay) - allowed)
    if unknown:
        raise GuardPolicyError(
            f"{where}: an overlay may only add {sorted(allowed)}, not {unknown}"
        )
    merged = dict(base)
    for key in ("compose_mutating_verbs", "docker_mutating_verbs"):
        merged[key] = _merge_unique(
            _strings(base.get(key), key), _strings(overlay.get(key), f"{where} {key}")
        )
    projects_raw = base.get("protected_projects")
    if not isinstance(projects_raw, list):
        raise GuardPolicyError("guard_policy.protected_projects must be a list")
    projects: list[dict[str, object]] = [
        dict(p) for p in projects_raw if isinstance(p, dict)
    ]
    extra_raw = overlay.get("protected_projects") or []
    if not isinstance(extra_raw, list):
        raise GuardPolicyError(f"{where} protected_projects must be a list")
    for entry in extra_raw:
        if not isinstance(entry, dict) or not isinstance(entry.get("project"), str):
            raise GuardPolicyError(
                f"{where}: each protected project needs a project name"
            )
        found = next(
            (p for p in projects if p.get("project") == entry["project"]), None
        )
        if found is None:
            projects.append(dict(entry))
            continue
        for key in ("compose_files", "containers"):
            found[key] = _merge_unique(
                _strings(found.get(key), key),
                _strings(entry.get(key), f"{where} {key}"),
            )
    merged["protected_projects"] = projects
    return merged


class ContractGuardPolicySource:
    """The contract's guard_policy, extended by every overlay found under the declared roots."""

    def __init__(self, contract_path: Path = CONTRACT_PATH) -> None:
        self._contract_path = contract_path

    def load(
        self, environ: Mapping[str, str] | None = None
    ) -> ModelDevRuntimeGuardPolicy:
        env = os.environ if environ is None else environ
        contract = _load_mapping(self._contract_path)
        raw = contract.get("guard_policy")
        if not isinstance(raw, dict):
            raise GuardPolicyError(f"{self._contract_path} has no guard_policy mapping")
        policy: dict[str, object] = dict(raw)
        overlay = find_node_overlay(NODE_NAME, env)
        if overlay is not None:
            policy = merge_overlay(policy, _load_mapping(overlay), str(overlay))
        try:
            return ModelDevRuntimeGuardPolicy.model_validate(policy)
        except ValueError as exc:
            raise GuardPolicyError(f"guard_policy does not validate: {exc}") from exc

    def handle(
        self, environ: Mapping[str, str] | None = None
    ) -> ModelDevRuntimeGuardPolicy:
        """Dispatch entrypoint: the policy for ``environ`` (the process environment by default)."""
        return self.load(environ)


__all__ = [
    "CONTRACT_PATH",
    "ContractGuardPolicySource",
    "GuardPolicyError",
    "merge_overlay",
]
