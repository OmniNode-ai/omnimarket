# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Shared routing tiers path authority and private harness overlay loader.

The delegation routing overlay resolves store-first (OMN-23826): the explicit
``overlay_path`` argument, then the ``delegation_routing`` document
(``<root>/delegation_routing/overlay.yaml``) under the first root in
``ONEX_SKILL_OVERLAY_ROOTS`` that holds one, then the file
``DELEGATION_ROUTING_OVERLAY_PATH`` names; nothing bound supplies the empty
overlay, with no packaged or home-directory file. A store document that fails
to load refuses rather than falling through to a lower source.

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

from omnimarket.handlers.node_overlay_reader import OVERLAY_ROOTS_ENV, find_node_overlay
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

#: The node whose overlay (``<root>/node_delegation_routing_reducer/overlay.yaml`` under
#: ``ONEX_SKILL_OVERLAY_ROOTS``) is a deployment's complete tier ladder (OMN-20935).
ROUTING_TIERS_OVERLAY_NODE = "node_delegation_routing_reducer"

#: The overlay store document (``<root>/delegation_routing/overlay.yaml`` under
#: ``ONEX_SKILL_OVERLAY_ROOTS``) the harness routing overlay is read from before
#: the ``DELEGATION_ROUTING_OVERLAY_PATH`` pin (OMN-23826).
ROUTING_OVERLAY_STORE_ID = "delegation_routing"

# OMN-15628: this is the single canonical routing_tiers.yaml location (the
# diverged omnibase_infra copy was deleted; this repo's packaged copy is the
# only source of truth). OMN-16200: it is also what an installed package
# resolves when DELEGATION_ROUTING_TIERS_PATH is unbound -- a customer's clean
# install has no deployment to bind the key, and the shipped ladder is the one
# it runs. The fallback is logged with provenance (source=bootstrap_default),
# never silent, exactly as the sibling TASK_CLASS_CONTRACT_PATH read already is.
#
# ``.parent`` x2 from ``src/omnimarket/handlers/routing/routing_tiers_path.py`` lands on
# ``src/omnimarket`` → ``src/omnimarket/configs/routing_tiers.yaml``, the single
# committed tiers file in this repo.
ROUTING_TIERS_PACKAGED_DEFAULT_PATH = (
    Path(__file__).parent.parent.parent / "configs" / "routing_tiers.yaml"
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

    OMN-20935: the tier ladder is a deployment's routing order, so between the pin
    and the packaged file sits the node overlay a deployment supplies:
    ``<root>/node_delegation_routing_reducer/overlay.yaml`` under the first root in
    ``ONEX_SKILL_OVERLAY_ROOTS`` that holds one. It is a complete ladder, not a patch.

    Returns:
        The env-pinned :class:`Path` from ``DELEGATION_ROUTING_TIERS_PATH`` when
        bound, otherwise the node overlay's ladder when a root supplies one,
        otherwise :data:`ROUTING_TIERS_PACKAGED_DEFAULT_PATH`.
    """
    config_path, _ = resolve_path_config(
        ROUTING_TIERS_PATH_ENV_KEY,
        ROUTING_TIERS_PACKAGED_DEFAULT_PATH,
        node_overlay=find_node_overlay(ROUTING_TIERS_OVERLAY_NODE),
    )
    return config_path


def _resolve_routing_overlay(
    overlay_path: Path | None,
) -> tuple[Path | None, str]:
    """The path of the delegation routing overlay and the key an error about it names."""
    if overlay_path is not None:
        return overlay_path, DELEGATION_ROUTING_OVERLAY_PATH_ENV_KEY
    store_overlay = find_node_overlay(ROUTING_OVERLAY_STORE_ID)
    if store_overlay is not None:
        return store_overlay, OVERLAY_ROOTS_ENV
    path, _ = resolve_optional_path_config(DELEGATION_ROUTING_OVERLAY_PATH_ENV_KEY)
    return path, DELEGATION_ROUTING_OVERLAY_PATH_ENV_KEY


def resolve_delegation_routing_overlay_path(
    overlay_path: Path | None = None,
) -> Path | None:
    """Resolve the delegation routing overlay path, store-first (OMN-23826).

    The ONE derivation of the order: the explicit ``overlay_path`` argument,
    then the ``delegation_routing`` document under the first root in
    ``ONEX_SKILL_OVERLAY_ROOTS`` that holds one, then the file
    ``DELEGATION_ROUTING_OVERLAY_PATH`` names.

    Returns:
        The resolved :class:`Path`, or ``None`` when nothing is bound; the
        caller then supplies the empty overlay.
    """
    return _resolve_routing_overlay(overlay_path)[0]


def load_delegation_routing_overlay(
    overlay_path: Path | None = None,
) -> ModelDelegationRoutingOverlay:
    """Load the delegation routing overlay (OMN-20287).

    Which harness backends a deployment has, the harness tiers they serve and
    each task class's escalation chain are deployment facts: the operator
    ruled that routing decisions come from overlays, never from this public
    package's packaged config. The package declares the overlay contract
    (:class:`ModelDelegationRoutingOverlay`) and its neutral default, the empty
    overlay, which is what an unbound selector resolves to. There is
    deliberately no home-directory fallback: a stray file must not route house
    traffic on a machine that never opted in.

    The path resolves through :func:`resolve_delegation_routing_overlay_path`:
    the explicit ``overlay_path``, then the ``delegation_routing`` document
    under ``ONEX_SKILL_OVERLAY_ROOTS`` (OMN-23826), then
    ``DELEGATION_ROUTING_OVERLAY_PATH``; nothing bound is the empty overlay.

    A bound selector (or an explicit ``overlay_path``) naming a missing,
    unreadable, non-YAML or invalid file raises
    :class:`ProtocolConfigurationError` naming the key and the path, rather
    than degrading to the empty overlay (rule 8: no silent config fallback).
    A broken store document refuses the same way, naming
    ``ONEX_SKILL_OVERLAY_ROOTS`` and the path, and never falls through to the
    pinned key: a deployment that ships a bad document must see it, not the
    file the key names behind it.
    """
    path, source = _resolve_routing_overlay(overlay_path)
    if path is None:
        return EMPTY_DELEGATION_ROUTING_OVERLAY
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            msg = "Expected YAML mapping at root"
            raise ValueError(msg)
        return ModelDelegationRoutingOverlay.model_validate(data)
    except (OSError, UnicodeError, yaml.YAMLError, ValidationError, ValueError) as exc:
        msg = f"{source} overlay at {path} is invalid: {exc}"
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
    "ROUTING_OVERLAY_STORE_ID",
    "ROUTING_TIERS_OVERLAY_NODE",
    "ROUTING_TIERS_PACKAGED_DEFAULT_PATH",
    "ROUTING_TIERS_PATH_ENV_KEY",
    "load_delegation_routing_overlay",
    "load_harness_tiers",
    "resolve_delegation_routing_overlay_path",
    "resolve_routing_tiers_path",
]
