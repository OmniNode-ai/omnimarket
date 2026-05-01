# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Runtime ownership guards for auto-wired omnimarket contracts."""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Any

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
NODES_ROOT = REPO_ROOT / "src" / "omnimarket" / "nodes"

_OWNERSHIP_KEYS = (
    "dependencies",
    "sub_handler_dependencies",
    "dependency_ownership",
    "dependency_owners",
    "runtime_dependencies",
    "requires_external_dependencies",
)


def _load_contract(path: Path) -> dict[str, Any]:
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    assert isinstance(raw, dict)
    return raw


def _runtime_profiles(raw: dict[str, Any]) -> tuple[str, ...]:
    profiles_raw = raw.get("runtime_profiles")
    descriptor_raw = raw.get("descriptor")
    if profiles_raw is None and isinstance(descriptor_raw, dict):
        profiles_raw = descriptor_raw.get("runtime_profiles")
    if profiles_raw is None:
        return ()
    if isinstance(profiles_raw, str):
        profiles = (profiles_raw,)
    else:
        assert isinstance(profiles_raw, list)
        profiles = tuple(profiles_raw)
    return tuple(profile.strip().lower() for profile in profiles)


def _declares_dependency_ownership(raw: dict[str, Any]) -> bool:
    for key in _OWNERSHIP_KEYS:
        if raw.get(key):
            return True
    descriptor_raw = raw.get("descriptor")
    if not isinstance(descriptor_raw, dict):
        return False
    return any(bool(descriptor_raw.get(key)) for key in _OWNERSHIP_KEYS)


def _module_path(module: str) -> Path | None:
    if not module.startswith("omnimarket."):
        return None
    return (REPO_ROOT / "src" / Path(*module.split("."))).with_suffix(".py")


def _contract_handler_refs(raw: dict[str, Any]) -> tuple[tuple[str, str], ...]:
    refs: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()

    def add(module: object, class_name: object) -> None:
        if not isinstance(module, str) or not isinstance(class_name, str):
            return
        if not module or not class_name.startswith("Handler"):
            return
        ref = (module, class_name)
        if ref not in seen:
            seen.add(ref)
            refs.append(ref)

    handler_raw = raw.get("handler")
    if isinstance(handler_raw, dict):
        add(handler_raw.get("module"), handler_raw.get("class"))

    routing_raw = raw.get("handler_routing")
    if isinstance(routing_raw, dict):
        for entry in routing_raw.get("handlers", []):
            if not isinstance(entry, dict):
                continue
            add(entry.get("handler_module"), entry.get("handler_class"))
            routed_handler = entry.get("handler")
            if isinstance(routed_handler, dict):
                add(routed_handler.get("module"), routed_handler.get("name"))

    return tuple(refs)


def _required_init_args(module: str, class_name: str) -> tuple[str, ...]:
    module_path = _module_path(module)
    if module_path is None or not module_path.exists():
        return ()

    tree = ast.parse(module_path.read_text(encoding="utf-8"))
    for node in tree.body:
        if not isinstance(node, ast.ClassDef) or node.name != class_name:
            continue
        for item in node.body:
            if not isinstance(item, ast.FunctionDef) or item.name != "__init__":
                continue
            positional = list(item.args.posonlyargs) + list(item.args.args)
            if positional and positional[0].arg == "self":
                positional = positional[1:]
            default_count = len(item.args.defaults)
            required_positional = (
                positional[: len(positional) - default_count]
                if default_count
                else positional
            )
            required = [arg.arg for arg in required_positional]
            required.extend(
                arg.arg
                for arg, default in zip(
                    item.args.kwonlyargs, item.args.kw_defaults, strict=True
                )
                if default is None
            )
            return tuple(required)
        return ()
    return ()


def _constructor_dependency_cases() -> tuple[Any, ...]:
    cases: list[Any] = []
    for contract_path in sorted(NODES_ROOT.glob("*/contract.yaml")):
        raw = _load_contract(contract_path)
        for module, class_name in _contract_handler_refs(raw):
            required_args = _required_init_args(module, class_name)
            if not required_args:
                continue
            cases.append(
                pytest.param(
                    contract_path,
                    class_name,
                    required_args,
                    id=f"{contract_path.parent.name}:{class_name}",
                )
            )
    return tuple(cases)


def _owned_by_runtime_profile(raw: dict[str, Any], profile: str) -> bool:
    profiles = _runtime_profiles(raw)
    return not profiles or profile.lower() in profiles


@pytest.mark.parametrize(
    ("contract_path", "handler_class", "required_args"),
    _constructor_dependency_cases(),
)
def test_nonstandard_handler_constructor_declares_runtime_ownership_or_dependencies(
    contract_path: Path,
    handler_class: str,
    required_args: tuple[str, ...],
) -> None:
    contract = _load_contract(contract_path)

    assert _runtime_profiles(contract) or _declares_dependency_ownership(contract), (
        f"{contract_path.parent.name}/{handler_class} requires constructor "
        f"dependencies {required_args}; declare runtime_profiles or dependency ownership"
    )


def test_main_profile_excludes_memory_and_intelligence_crashers() -> None:
    intent_consumer = _load_contract(
        NODES_ROOT / "node_intent_event_consumer_effect" / "contract.yaml"
    )
    intelligence_orchestrator = _load_contract(
        NODES_ROOT / "node_intelligence_orchestrator" / "contract.yaml"
    )

    assert not _owned_by_runtime_profile(intent_consumer, "main")
    assert _runtime_profiles(intent_consumer) == ("memory",)
    assert not _owned_by_runtime_profile(intelligence_orchestrator, "main")
    assert _runtime_profiles(intelligence_orchestrator) == ("intelligence",)
