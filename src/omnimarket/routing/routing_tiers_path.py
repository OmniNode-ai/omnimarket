# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Shared routing tiers path authority (OMN-15628).

The ONE derivation of where the delegation routing tier ladder lives. Both the
routing authority (``node_delegation_routing_reducer``, which parses the file
into the tier config) and the delegation orchestrator
(``node_delegation_orchestrator``, which records the file's sha256 as replay
provenance) import from here, so neither node depends on the other's handler
package.

Why this module exists: the orchestrator previously re-derived the path with
its own ``Path(__file__).parent`` walk. It was off by one ``.parent``, pointed
at a nonexistent ``src/configs/routing_tiers.yaml``, and never read the
``DELEGATION_ROUTING_TIERS_PATH`` env pin — so the provenance hash on every
terminal delegation result was silently ``None``. Two derivations of one shape
is the defect; this module is the single derivation that replaces them.
"""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import ValidationError

from omnimarket.inference.delegation_config_provenance import resolve_path_config
from omnimarket.models.delegation.model_harness_tier import ModelHarnessTier

#: Env key a contract overlay / deployment MUST bind to pin the tiers file.
ROUTING_TIERS_PATH_ENV_KEY = "DELEGATION_ROUTING_TIERS_PATH"

# OMN-15628: this is the single canonical routing_tiers.yaml location (the
# diverged omnibase_infra copy was deleted; this repo's packaged copy is the
# only source of truth). OMN-16200: it is also what an installed package
# resolves when DELEGATION_ROUTING_TIERS_PATH is unbound -- a customer's clean
# install has no deployment to bind the key, and the shipped ladder is the one
# it runs. The fallback is logged with provenance (source=bootstrap_default),
# never silent, exactly as the sibling TASK_CLASS_CONTRACT_PATH read already is.
#
# ``.parent`` x2 from ``src/omnimarket/routing/routing_tiers_path.py`` lands on
# ``src/omnimarket`` → ``src/omnimarket/configs/routing_tiers.yaml``, the single
# committed tiers file in this repo.
ROUTING_TIERS_PACKAGED_DEFAULT_PATH = (
    Path(__file__).parent.parent / "configs" / "routing_tiers.yaml"
)


def resolve_routing_tiers_path() -> Path:
    """Resolve the ``routing_tiers.yaml`` path the routing authority reads.

    OMN-15628. The SINGLE canonical derivation of this path — every surface that
    needs to know which tiers file is in force (the config loader, the
    delegation orchestrator's replay-provenance ``routing_tiers_hash``) calls
    THIS function instead of walking ``Path(__file__).parent`` itself. The
    orchestrator's private re-derivation was off by one ``.parent`` and pointed
    at a nonexistent ``src/configs/routing_tiers.yaml``, silently nulling the
    provenance hash; one derivation per shape is the fix.

    OMN-16200: an unbound key resolves to the packaged file rather than
    refusing. The refusal left a clean install with no way to delegate at all:
    the customer's first ``onex delegate`` died naming an env var nothing they
    installed documents, while the file it wanted ships inside the wheel. The
    choice is recorded, not hidden -- :func:`resolve_path_config` logs a
    ``source=bootstrap_default`` provenance line naming the resolved path, and
    a deployment that binds the key still gets exactly the file it bound.

    Returns:
        The env-pinned :class:`Path` from ``DELEGATION_ROUTING_TIERS_PATH`` when
        bound, otherwise :data:`ROUTING_TIERS_PACKAGED_DEFAULT_PATH`.
    """
    config_path, _ = resolve_path_config(
        ROUTING_TIERS_PATH_ENV_KEY, ROUTING_TIERS_PACKAGED_DEFAULT_PATH
    )
    return config_path


def load_harness_tiers(
    routing_tiers_path: Path | None = None,
) -> tuple[ModelHarnessTier, ...]:
    """Read the ``harness_tiers`` block of the routing tiers file (OMN-20287).

    The ladder parser reads only ``tiers``, so the harness tiers are read here,
    from the same resolved file. Absent block -> ``()``; a malformed entry or a
    duplicate tier name raises ``ValueError`` naming the tier and the path.
    """
    path = (
        routing_tiers_path
        if routing_tiers_path is not None
        else resolve_routing_tiers_path()
    )
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        msg = f"Expected YAML mapping at root for {path}"
        raise ValueError(msg)
    if "harness_tiers" not in data:
        return ()
    block = data["harness_tiers"]
    if not isinstance(block, list):
        msg = f"harness_tiers must be a list in {path}"
        raise ValueError(msg)
    tiers: list[ModelHarnessTier] = []
    seen: set[str] = set()
    for index, entry in enumerate(block):
        name = (
            entry.get("name", f"<entry {index}>")
            if isinstance(entry, dict)
            else f"<entry {index}>"
        )
        try:
            tier = ModelHarnessTier.model_validate(entry)
        except ValidationError as exc:
            msg = f"Harness tier {name!r} is invalid in {path}: {exc}"
            raise ValueError(msg) from exc
        if tier.name in seen:
            msg = f"Duplicate harness tier {tier.name!r} in {path}"
            raise ValueError(msg)
        seen.add(tier.name)
        tiers.append(tier)
    return tuple(tiers)


__all__ = [
    "ROUTING_TIERS_PACKAGED_DEFAULT_PATH",
    "ROUTING_TIERS_PATH_ENV_KEY",
    "load_harness_tiers",
    "resolve_routing_tiers_path",
]
