# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Shared routing tiers path authority and private harness overlay loader.

Harness routing comes only from DELEGATION_ROUTING_OVERLAY_PATH; an unbound
selector supplies an empty overlay, with no packaged or home-directory file.

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
from omnibase_infra.errors import ProtocolConfigurationError
from pydantic import ValidationError

from omnimarket.inference.delegation_config_provenance import (
    DELEGATION_ROUTING_OVERLAY_CONFIG_KEY,
    resolve_optional_path_config,
    resolve_path_config,
)
from omnimarket.models.delegation.model_delegation_routing_overlay import (
    EMPTY_DELEGATION_ROUTING_OVERLAY,
    ModelDelegationRoutingOverlay,
)
from omnimarket.models.delegation.model_harness_tier import ModelHarnessTier

#: Env key a contract overlay / deployment MUST bind to pin the tiers file.
ROUTING_TIERS_PATH_ENV_KEY = "DELEGATION_ROUTING_TIERS_PATH"
DELEGATION_ROUTING_OVERLAY_PATH_ENV_KEY = DELEGATION_ROUTING_OVERLAY_CONFIG_KEY

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


def load_delegation_routing_overlay(
    overlay_path: Path | None = None,
) -> ModelDelegationRoutingOverlay:
    """Load the delegation routing overlay (OMN-20287).

    Which harness backends a deployment has, the harness tiers they serve and
    each task class's escalation chain are deployment facts: the operator
    ruled that routing decisions come from overlays, never from this public
    package's packaged config. The package declares the overlay contract
    (:class:`ModelDelegationRoutingOverlay`) and its neutral default, the empty
    overlay, which is what an unbound ``DELEGATION_ROUTING_OVERLAY_PATH``
    resolves to. There is deliberately no home-directory fallback: a stray file
    must not route house traffic on a machine that never opted in.

    A bound selector (or an explicit ``overlay_path``) naming a missing,
    unreadable, non-YAML or invalid file raises
    :class:`ProtocolConfigurationError` naming the key and the path, rather
    than degrading to the empty overlay (rule 8: no silent config fallback).
    """
    path = overlay_path
    if path is None:
        path, _ = resolve_optional_path_config(DELEGATION_ROUTING_OVERLAY_PATH_ENV_KEY)
    if path is None:
        return EMPTY_DELEGATION_ROUTING_OVERLAY
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            msg = "Expected YAML mapping at root"
            raise ValueError(msg)
        return ModelDelegationRoutingOverlay.model_validate(data)
    except (OSError, UnicodeError, yaml.YAMLError, ValidationError, ValueError) as exc:
        msg = f"{DELEGATION_ROUTING_OVERLAY_PATH_ENV_KEY} overlay at {path} is invalid: {exc}"
        raise ProtocolConfigurationError(msg) from exc


def load_harness_tiers(
    overlay: ModelDelegationRoutingOverlay | None = None,
) -> tuple[ModelHarnessTier, ...]:
    """Return the harness tiers of the delegation routing overlay (OMN-20287).

    Harness tiers are deployment facts, so the packaged ``routing_tiers.yaml``
    declares none; they come only from the overlay
    :func:`load_delegation_routing_overlay` resolves. Unbound selector -> ``()``.
    """
    resolved = overlay if overlay is not None else load_delegation_routing_overlay()
    return resolved.harness_tiers


__all__ = [
    "DELEGATION_ROUTING_OVERLAY_PATH_ENV_KEY",
    "ROUTING_TIERS_PACKAGED_DEFAULT_PATH",
    "ROUTING_TIERS_PATH_ENV_KEY",
    "load_delegation_routing_overlay",
    "load_harness_tiers",
    "resolve_routing_tiers_path",
]
