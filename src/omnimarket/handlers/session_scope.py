# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Bind a session to the ledger principals it may act for.

Session recovery (listing dangling CLAIMs, closing or relaunching them, reading and writing session
checkpoints) acts for one principal at a time. The binding comes from the team identity declaration
through ``node_team_identity_compute``'s loader and resolver, never from an environment string:

* an authenticated match of the one operator entry is an operator session, which acts on every
  lane and checkpoint as before;
* an authenticated match of a member entry is a member session, which acts only for that entry's
  ledger principals;
* anything else (no claims, environment-only claims, an unknown or mismatched identity, a missing
  or malformed declaration) is a member session with no principals at all: it may read, but it owns
  nothing and so acts on nothing.
"""

from __future__ import annotations

from collections.abc import Sequence

from omnimarket.models.session_scope import ModelSessionScope
from omnimarket.nodes.node_team_identity_compute.handlers import (
    TeamIdentityError,
    load_team_identity,
    resolve_role,
)
from omnimarket.nodes.node_team_identity_compute.models import ModelIdentityClaim


def _restricted(diagnostic: str, sha256: str | None) -> ModelSessionScope:
    return ModelSessionScope(
        privileges="team",
        entry_id=None,
        principals=(),
        writer_principal=None,
        diagnostic=diagnostic,
        source_sha256=sha256,
    )


def bind_session_scope(
    claims: Sequence[ModelIdentityClaim],
    overlay_roots: Sequence[str] | None = None,
) -> ModelSessionScope:
    """Resolve ``claims`` against the team identity declaration under ``overlay_roots``.

    ``overlay_roots`` ``None`` reads ``ONEX_SKILL_OVERLAY_ROOTS``. Never raises for an identity
    problem: every failure is a restricted member scope carrying the diagnostic.
    """
    try:
        resolved = load_team_identity(overlay_roots)
    except TeamIdentityError as exc:
        return _restricted(f"{exc}; member restrictions apply", None)
    result = resolve_role(resolved, claims)
    sha256 = resolved.source.sha256
    if result.entry_id is None:
        return _restricted(
            f"{result.diagnostic or 'no identity'}; member restrictions apply", sha256
        )
    entry = next(e for e in resolved.overlay.entries if e.id == result.entry_id)
    principals = tuple(entry.ledger_principals)
    bound = [
        claim.value
        for claim in claims
        if claim.source == "broker_binding" and claim.value in principals
    ]
    return ModelSessionScope(
        privileges=result.privileges,
        entry_id=entry.id,
        principals=principals,
        writer_principal=bound[0] if bound else principals[0],
        diagnostic=result.diagnostic,
        source_sha256=sha256,
    )
