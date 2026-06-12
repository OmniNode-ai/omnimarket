#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Gemini registry/version hygiene gate (OMN-12972, plan P2.7).

Enforces that the model registry and the bifrost delegation backends declare
Gemini-family served model names that are VALID FOR THE ENVIRONMENT THAT SERVES
THEM, and that no entry carries the dead ``gemini-2.0-flash`` name that 404s on
Vertex (the OMN-12937 failure that triggered the flash-lite retarget).

The gate is a CONTRACT/CONFIG check — it reads only committed YAML, no network.

It FAILS (exit 1) when, for any ``provider: google`` registry entry:

  1. ``model_name`` or any ``served_model_names`` value contains a DEAD model id
     (``gemini-2.0-flash`` / ``gemini-1.5-*``) — the names that 404 on Vertex.

  2. ``served_model_names`` is declared but MALFORMED for the per-environment
     contract:
       * a ``vertex`` entry that is NOT publisher-qualified
         (``publishers/google/models/<name>``) — Vertex 404s on a bare name.
       * an ``ai_studio`` entry that IS publisher-qualified — AI Studio
         (generativelanguage) 404s on the Vertex resource path.
       * the ``ai_studio`` served name disagreeing with the entry ``model_name``
         (the single-name ``model_name`` MUST be the AI-Studio default so legacy
         single-name consumers and the per-env map cannot drift).

  3. the bifrost backend that routes this registry entry (matched by
     ``model_name``) declares a DIFFERENT ``model_name`` than the registry —
     registry↔tier drift, the exact P2.7 hazard ("registry now flash-lite" while
     a backend still points elsewhere).

Usage:
    uv run python scripts/ci/check_model_registry_served_names.py          # enforce
    uv run python scripts/ci/check_model_registry_served_names.py --json   # JSON packet
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

import yaml

_REPO_ROOT_MARKER = ".git"

_REGISTRY_REL = "src/omnimarket/data/model_registry/model_registry_v1.yaml"
_BIFROST_REL = "src/omnimarket/configs/bifrost_delegation.yaml"

# Model ids that are known to 404 / be retired on the live Vertex environment.
# Any appearance of these in a google registry entry's served name is a defect:
# the registry must track the live Vertex-served family, not a dead version.
_DEAD_GEMINI_MODEL_IDS: tuple[str, ...] = (
    "gemini-2.0-flash",
    "gemini-2.0-flash-lite",
    "gemini-1.5-flash",
    "gemini-1.5-pro",
)

# Environment id under which Vertex serves models. Vertex requires the
# publisher-qualified resource path.
_VERTEX_ENV_ID = "vertex"
# Environment id for AI Studio (generativelanguage) — serves the bare name.
_AI_STUDIO_ENV_ID = "ai_studio"

_VERTEX_PUBLISHER_PREFIX = "publishers/google/models/"


def _find_repo_root(start: Path) -> Path:
    candidate = start
    while candidate != candidate.parent:
        if (candidate / _REPO_ROOT_MARKER).exists():
            return candidate
        candidate = candidate.parent
    return start


def _load_yaml_mapping(path: Path) -> dict[str, Any]:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{path}: expected a top-level mapping")
    return data


def _bifrost_model_names_by_model(bifrost: dict[str, Any]) -> dict[str, set[str]]:
    """Map each backend model_name to the set of backend_ids declaring it.

    Used to detect registry↔backend drift: a registry google entry's model_name
    must be served by at least one bifrost backend, and no backend may claim that
    same registry model_name under a conflicting served name.
    """
    out: dict[str, set[str]] = {}
    backends = bifrost.get("backends", [])
    if isinstance(backends, list):
        for backend in backends:
            if not isinstance(backend, dict):
                continue
            model_name = backend.get("model_name")
            backend_id = backend.get("backend_id")
            if isinstance(model_name, str) and isinstance(backend_id, str):
                out.setdefault(model_name, set()).add(backend_id)
    return out


def evaluate(repo_root: Path) -> dict[str, Any]:
    registry_path = repo_root / _REGISTRY_REL
    bifrost_path = repo_root / _BIFROST_REL

    errors: list[str] = []
    checked: list[str] = []

    if not registry_path.exists():
        return {
            "passed": False,
            "checked": [],
            "errors": [f"registry not found: {_REGISTRY_REL}"],
        }
    if not bifrost_path.exists():
        return {
            "passed": False,
            "checked": [],
            "errors": [f"bifrost delegation config not found: {_BIFROST_REL}"],
        }

    registry = _load_yaml_mapping(registry_path)
    bifrost = _load_yaml_mapping(bifrost_path)
    bifrost_model_names = _bifrost_model_names_by_model(bifrost)

    models = registry.get("models", {})
    if not isinstance(models, dict):
        return {
            "passed": False,
            "checked": [],
            "errors": [f"{_REGISTRY_REL}: 'models' must be a mapping"],
        }

    for model_id, profile in models.items():
        if not isinstance(profile, dict):
            errors.append(f"{_REGISTRY_REL}: model {model_id!r} is not a mapping")
            continue
        if profile.get("provider") != "google":
            continue
        checked.append(model_id)

        model_name = profile.get("model_name")
        served = profile.get("served_model_names")

        # Collect every served name declared on this google entry.
        declared_names: list[tuple[str, str]] = []
        if isinstance(model_name, str):
            declared_names.append(("model_name", model_name))
        if isinstance(served, dict):
            for env_id, name in served.items():
                if isinstance(name, str):
                    declared_names.append((f"served_model_names[{env_id}]", name))

        # (1) Dead-model-id ban.
        for where, name in declared_names:
            for dead in _DEAD_GEMINI_MODEL_IDS:
                if name == dead or name.endswith("/" + dead):
                    errors.append(
                        f"{_REGISTRY_REL}: model {model_id!r} {where}={name!r} "
                        f"references retired/404 model id {dead!r}; align to the "
                        "live Vertex-served family"
                    )

        # (2) Per-environment served-name well-formedness.
        if isinstance(served, dict):
            vertex_name = served.get(_VERTEX_ENV_ID)
            ai_studio_name = served.get(_AI_STUDIO_ENV_ID)

            if isinstance(vertex_name, str) and not vertex_name.startswith(
                _VERTEX_PUBLISHER_PREFIX
            ):
                errors.append(
                    f"{_REGISTRY_REL}: model {model_id!r} served_model_names"
                    f"[{_VERTEX_ENV_ID}]={vertex_name!r} must be publisher-qualified "
                    f"({_VERTEX_PUBLISHER_PREFIX}<name>); a bare name 404s on Vertex"
                )
            if isinstance(ai_studio_name, str) and ai_studio_name.startswith(
                _VERTEX_PUBLISHER_PREFIX
            ):
                errors.append(
                    f"{_REGISTRY_REL}: model {model_id!r} served_model_names"
                    f"[{_AI_STUDIO_ENV_ID}]={ai_studio_name!r} must be the bare "
                    "served name; AI Studio (generativelanguage) 404s on the "
                    "Vertex publisher resource path"
                )
            if (
                isinstance(ai_studio_name, str)
                and isinstance(model_name, str)
                and ai_studio_name != model_name
            ):
                errors.append(
                    f"{_REGISTRY_REL}: model {model_id!r} model_name={model_name!r} "
                    f"must equal served_model_names[{_AI_STUDIO_ENV_ID}]="
                    f"{ai_studio_name!r} (model_name is the AI-Studio default; the "
                    "two must not drift)"
                )

        # (3) Registry↔backend drift — the entry's model_name must be served by a
        # bifrost backend (no orphan google registry entry whose served name no
        # backend routes).
        if isinstance(model_name, str) and model_name not in bifrost_model_names:
            errors.append(
                f"{_BIFROST_REL}: no backend declares model_name={model_name!r} "
                f"for registry google entry {model_id!r}; registry↔tier drift "
                "(a registry served name with no routing backend)"
            )

    passed = len(errors) == 0 and len(checked) > 0
    if len(checked) == 0:
        errors.append(
            f"{_REGISTRY_REL}: no provider:google entries found — the gate must "
            "have at least one google entry to verify"
        )
        passed = False

    return {"passed": passed, "checked": checked, "errors": errors}


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Gemini registry/version hygiene gate (OMN-12972, plan P2.7)"
    )
    parser.add_argument(
        "--json", action="store_true", help="print the evidence packet as JSON"
    )
    args = parser.parse_args()

    repo_root = _find_repo_root(Path.cwd())
    packet = evaluate(repo_root)
    packet["ticket"] = "OMN-12972"
    packet["gate"] = "gemini-registry-version-hygiene"

    if args.json:
        print(json.dumps(packet, indent=2, sort_keys=True))
        return 0 if packet["passed"] else 1

    if packet["passed"]:
        print(
            "[gemini-registry-hygiene] PASS — "
            f"{len(packet['checked'])} google registry entry(ies) verified: "
            f"{', '.join(packet['checked'])}"
        )
        return 0

    print("[gemini-registry-hygiene] FAIL")
    for err in packet["errors"]:
        print(f"  {err}")
    print(
        "\nFix: align every provider:google registry entry + its bifrost backend "
        "with the live Vertex-served model name PER ENVIRONMENT — Vertex names are "
        "publisher-qualified (publishers/google/models/<name>), AI Studio names are "
        "bare, and no entry may reference a retired/404 model id."
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
