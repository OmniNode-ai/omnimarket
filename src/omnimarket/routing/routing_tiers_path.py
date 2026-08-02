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

from omnimarket.inference.delegation_config_provenance import (
    resolve_required_path_config,
)

#: Env key a contract overlay / deployment MUST bind to pin the tiers file.
ROUTING_TIERS_PATH_ENV_KEY = "DELEGATION_ROUTING_TIERS_PATH"

# OMN-15628: this is the single canonical routing_tiers.yaml location (the
# diverged omnibase_infra copy was deleted; this repo's packaged copy is the
# only source of truth). ``_get_config()`` does NOT default to it silently — a
# caller/deployment must bind DELEGATION_ROUTING_TIERS_PATH explicitly (rule 8,
# no invisible env config). This constant exists for the callers that legitimately
# need the packaged path: tests and deploy tooling constructing that binding, and
# non-fatal provenance recording that must not abort on an unbound key.
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

    Returns:
        The env-pinned :class:`Path` from ``DELEGATION_ROUTING_TIERS_PATH``.

    Raises:
        ValueError: If ``DELEGATION_ROUTING_TIERS_PATH`` is unset or blank.
            There is deliberately no packaged-default fallback here (rule 8 —
            no invisible env config); callers that cannot fail (provenance
            recording) fall back to
            :data:`ROUTING_TIERS_PACKAGED_DEFAULT_PATH` explicitly.
    """
    config_path, _ = resolve_required_path_config(ROUTING_TIERS_PATH_ENV_KEY)
    return config_path


__all__ = [
    "ROUTING_TIERS_PACKAGED_DEFAULT_PATH",
    "ROUTING_TIERS_PATH_ENV_KEY",
    "resolve_routing_tiers_path",
]
