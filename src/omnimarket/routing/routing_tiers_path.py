# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Shared routing tiers path authority."""

from __future__ import annotations

from pathlib import Path

from omnimarket.inference.delegation_config_provenance import (
    resolve_required_path_config,
)

# OMN-15628: this is the single canonical routing_tiers.yaml location (the
# diverged omnibase_infra copy was deleted; this repo's packaged copy is the
# only source of truth). ``_get_config()`` no longer defaults to it silently —
# a caller/deployment must bind DELEGATION_ROUTING_TIERS_PATH explicitly (rule
# 8, no invisible env config). Kept as a constant for callers (tests, deploy
# tooling) that need the canonical packaged path to construct that binding.
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
    config_path, _ = resolve_required_path_config("DELEGATION_ROUTING_TIERS_PATH")
    return config_path
