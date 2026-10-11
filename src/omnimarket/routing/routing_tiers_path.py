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
from omnimarket.models.node_overlay.node_overlay_reader import (
    NodeOverlayError,
    find_node_overlay,
)

#: Env key a contract overlay / deployment MUST bind to pin the tiers file.
ROUTING_TIERS_PATH_ENV_KEY = "DELEGATION_ROUTING_TIERS_PATH"
DELEGATION_ROUTING_OVERLAY_PATH_ENV_KEY = DELEGATION_ROUTING_OVERLAY_CONFIG_KEY

# The packaged routing_tiers.yaml is the NEUTRAL DEFAULT ladder: the tier schema and
# one `local` tier on capability-named backends, with no host, vendor, metered
# tier or evaluator model (operator rulings 2026-09-26, 2026-09-30, 2026-10-09 and
# 2026-10-10: a deployment's routing order lives in its overlay, never in the
# package). OMN-16200: it is also what an installed package resolves when nothing
# else is supplied -- a customer's clean install has no deployment to bind the
# key, and declares its own local model in its bifrost overlay. The fallback is
# logged with provenance (source=bootstrap_default), never silent, exactly as the
# sibling TASK_CLASS_CONTRACT_PATH read already is.
#
# ``.parent`` x2 from ``src/omnimarket/routing/routing_tiers_path.py`` lands on
# ``src/omnimarket`` → ``src/omnimarket/configs/routing_tiers.yaml``, the single
# committed tiers file in this repo.
ROUTING_TIERS_PACKAGED_DEFAULT_PATH = (
    Path(__file__).parent.parent / "configs" / "routing_tiers.yaml"
)

#: The node whose ``overlay.yaml`` under ``ONEX_SKILL_OVERLAY_ROOTS`` is a
#: deployment's own ladder, a file of exactly the shape the packaged one has.
ROUTING_TIERS_OVERLAY_NODE = "node_delegation_routing_reducer"


def resolve_routing_tiers_path() -> Path:
    """Resolve the ``routing_tiers.yaml`` path the routing authority reads.

    OMN-15628. The SINGLE canonical derivation of this path — every surface that
    needs to know which tiers file is in force (the config loader, the
    delegation orchestrator's replay-provenance ``routing_tiers_hash``) calls
    THIS function instead of walking ``Path(__file__).parent`` itself. The
    orchestrator's private re-derivation was off by one ``.parent`` and pointed
    at a nonexistent ``src/configs/routing_tiers.yaml``, silently nulling the
    provenance hash; one derivation per shape is the fix.

    Resolution order: the file ``DELEGATION_ROUTING_TIERS_PATH`` pins, then the
    deployment's ladder at
    ``<root>/node_delegation_routing_reducer/overlay.yaml`` under the first
    directory of ``ONEX_SKILL_OVERLAY_ROOTS`` that holds one, then the packaged
    neutral default. The choice is recorded, not hidden --
    :func:`resolve_path_config` logs a ``source=bootstrap_default`` provenance
    line naming the resolved path, and a deployment that binds the key or
    supplies the overlay gets exactly the file it supplied.

    Returns:
        The env-pinned :class:`Path` from ``DELEGATION_ROUTING_TIERS_PATH`` when
        bound, else the overlay's ladder when one is supplied, otherwise
        :data:`ROUTING_TIERS_PACKAGED_DEFAULT_PATH`.
    """
    pinned, _ = resolve_optional_path_config(ROUTING_TIERS_PATH_ENV_KEY)
    if pinned is not None:
        return pinned
    try:
        overlay = find_node_overlay(ROUTING_TIERS_OVERLAY_NODE)
    except NodeOverlayError as exc:
        raise ProtocolConfigurationError(str(exc)) from exc
    if overlay is not None:
        return overlay
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
    "ROUTING_TIERS_OVERLAY_NODE",
    "ROUTING_TIERS_PACKAGED_DEFAULT_PATH",
    "ROUTING_TIERS_PATH_ENV_KEY",
    "load_delegation_routing_overlay",
    "load_harness_tiers",
    "resolve_routing_tiers_path",
]
