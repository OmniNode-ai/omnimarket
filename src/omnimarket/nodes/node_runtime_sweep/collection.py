# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Contract-collection harness for node_runtime_sweep (OMN-13919).

This module is the I/O boundary that walks ``$OMNI_HOME`` repos and turns
``contract.yaml`` files into :class:`ModelContractInput` entries. It is shared
by BOTH executable paths:

* ``python -m omnimarket.nodes.node_runtime_sweep`` (the ``__main__`` CLI), and
* the ``onex skill runtime_sweep`` dispatch path, where the handler resolves
  a default input set when the caller supplied no entities at all.

Before OMN-13919 this logic lived only in ``__main__.py``, so the skill
dispatch path invoked the pure handler with an empty request and every
``onex skill runtime_sweep`` run reported ``status=no_input`` with zero
entities checked — the vacuous-pass class OMN-13715/OMN-13708 was meant to
eliminate.
"""

from __future__ import annotations

import logging
from pathlib import Path

import yaml

from omnimarket.nodes.node_runtime_sweep.handlers.handler_runtime_sweep import (
    ModelContractInput,
)

__all__ = ["collect_contracts", "extract_runtime_profiles"]

_log = logging.getLogger(__name__)


def extract_runtime_profiles(raw: dict[str, object]) -> list[str]:
    """Return declared runtime_profiles (top-level or under descriptor), lower-cased."""
    profiles_raw = raw.get("runtime_profiles")
    descriptor = raw.get("descriptor")
    if profiles_raw is None and isinstance(descriptor, dict):
        profiles_raw = descriptor.get("runtime_profiles")
    if isinstance(profiles_raw, str):
        candidates: list[object] = [profiles_raw]
    elif isinstance(profiles_raw, (list, tuple)):
        candidates = list(profiles_raw)
    else:
        return []
    return [p.strip().lower() for p in candidates if isinstance(p, str) and p.strip()]


def collect_contracts(omni_home: str, scope: str) -> list[ModelContractInput]:
    """Walk omni_home repos and collect contract.yaml definitions."""
    root = Path(omni_home)
    contracts: list[ModelContractInput] = []

    if scope == "omnidash-only":
        repos = ["omnidash"]
    else:
        repos = [
            d.name
            for d in root.iterdir()
            if d.is_dir() and not d.name.startswith(".") and (d / "src").exists()
        ]

    for repo in repos:
        repo_dir = root / repo
        if not repo_dir.is_dir():
            continue
        for contract_path in repo_dir.rglob("contract.yaml"):
            if "nodes" not in str(contract_path):
                continue
            try:
                raw = yaml.safe_load(contract_path.read_text())
                if not isinstance(raw, dict):
                    continue
                name = raw.get("name", contract_path.parent.name)
                description = raw.get("description", "")
                handler_spec = raw.get("handler", {})
                handler_module = (
                    handler_spec.get("module", "")
                    if isinstance(handler_spec, dict)
                    else ""
                )
                event_bus = raw.get("event_bus", {})
                raw_publish = (
                    event_bus.get("publish_topics", [])
                    if isinstance(event_bus, dict)
                    else []
                )
                raw_subscribe = (
                    event_bus.get("subscribe_topics", [])
                    if isinstance(event_bus, dict)
                    else []
                )
                # Only include string topics (skip structured event model entries)
                publish_topics = [t for t in (raw_publish or []) if isinstance(t, str)]
                subscribe_topics = [
                    t for t in (raw_subscribe or []) if isinstance(t, str)
                ]
                contracts.append(
                    ModelContractInput(
                        node_name=name,
                        description=description.strip() if description else "",
                        handler_module=handler_module,
                        publish_topics=publish_topics,
                        subscribe_topics=subscribe_topics,
                        runtime_profiles=extract_runtime_profiles(raw),
                    )
                )
            except Exception as exc:
                _log.warning("failed to parse %s: %s", contract_path, exc)

    return contracts
