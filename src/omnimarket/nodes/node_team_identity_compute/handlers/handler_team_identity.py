# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Load the team identity declaration and resolve a caller's role from it.

The loader reads ``<root>/team_identity/overlay.yaml`` under the roots of ``ONEX_SKILL_OVERLAY_ROOTS``,
the overlay-selection convention of :mod:`omnimarket.handlers.node_overlay_reader`. Unlike a
first-root-wins setting, who the operator is must not depend on root order: two roots holding
different declarations are refused as conflicting sources, and identical copies resolve to the
first. The resolved path and the SHA-256 of its bytes travel with the declaration so callers can
keep them in their proof receipts.

The resolver never fails open. An unknown, unavailable, mismatched or unauthenticated claim, and a
missing or malformed declaration, all resolve to team privileges with a diagnostic; only an
authenticated claim matching the one operator entry resolves to operator privileges.
"""

from __future__ import annotations

import hashlib
import os
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path

import yaml
from pydantic import ValidationError

from omnimarket.handlers.node_overlay_reader import (
    OVERLAY_FILE_NAME,
    OVERLAY_ROOTS_ENV,
    NodeOverlayError,
    not_configured_message,
)

from ..models.model_team_identity import (
    TEAM_IDENTITY_SCHEMA_VERSION,
    ModelIdentityClaim,
    ModelResolvedTeamIdentity,
    ModelTeamIdentityEntry,
    ModelTeamIdentityOverlay,
    ModelTeamIdentityRequest,
    ModelTeamIdentityResult,
    ModelTeamIdentitySource,
)

OVERLAY_ID = "team_identity"

# The entry field each authenticated claim source is matched against.
_CLAIM_FIELDS: Mapping[str, str] = {
    "slack_auth": "slack_member_id",
    "linear_auth": "linear_user_id",
    "github_auth": "github_login",
    "broker_binding": "ledger_principals",
}


class TeamIdentityError(NodeOverlayError):
    """The team identity declaration is absent, conflicting or invalid."""


def overlay_roots_from_env(environ: Mapping[str, str] | None = None) -> tuple[str, ...]:
    """The roots named in ``ONEX_SKILL_OVERLAY_ROOTS``, in order, blanks dropped."""
    env = os.environ if environ is None else environ
    return tuple(
        root for root in env.get(OVERLAY_ROOTS_ENV, "").split(os.pathsep) if root
    )


def find_team_identity_sources(roots: Sequence[str]) -> tuple[Path, ...]:
    """Every ``<root>/team_identity/overlay.yaml`` present, in root order."""
    return tuple(
        candidate
        for candidate in (Path(root) / OVERLAY_ID / OVERLAY_FILE_NAME for root in roots)
        if candidate.is_file()
    )


def _read(path: Path) -> bytes:
    try:
        return path.read_bytes()
    except OSError as exc:
        raise TeamIdentityError(
            f"{OVERLAY_ROOTS_ENV}: overlay {path} is unreadable ({type(exc).__name__})"
        ) from None


def load_team_identity(
    roots: Sequence[str] | None = None,
    *,
    environ: Mapping[str, str] | None = None,
) -> ModelResolvedTeamIdentity:
    """Load and validate the declaration; ``roots`` ``None`` reads ``ONEX_SKILL_OVERLAY_ROOTS``.

    Raises :class:`TeamIdentityError` when no root holds one, when roots hold differing
    declarations, or when the selected one is unreadable or invalid. Messages name the source,
    fields and entry positions, never values.
    """
    resolved_roots = overlay_roots_from_env(environ) if roots is None else tuple(roots)
    sources = find_team_identity_sources(resolved_roots)
    if not sources:
        raise TeamIdentityError(not_configured_message(OVERLAY_ID, "entries"))
    contents = [(path, _read(path)) for path in sources]
    digests = {path: hashlib.sha256(data).hexdigest() for path, data in contents}
    if len(set(digests.values())) > 1:
        raise TeamIdentityError(
            f"{OVERLAY_ROOTS_ENV}: conflicting {OVERLAY_ID} overlays at "
            f"{[str(path) for path in sources]}; exactly one declaration may be in force"
        )
    path, data = contents[0]
    source = f"{OVERLAY_ROOTS_ENV}: overlay {path}"
    try:
        raw = yaml.safe_load(data.decode("utf-8"))
    except (UnicodeError, yaml.YAMLError) as exc:
        raise TeamIdentityError(
            f"{source} is unreadable ({type(exc).__name__})"
        ) from None
    try:
        overlay = ModelTeamIdentityOverlay.model_validate(raw)
    except ValidationError as exc:
        raise TeamIdentityError(
            f"{source} is invalid for {OVERLAY_ID} (ValidationError): "
            + "; ".join(_describe(exc))
        ) from None
    return ModelResolvedTeamIdentity(
        overlay=overlay,
        source=ModelTeamIdentitySource(
            path=str(path),
            sha256=digests[path],
            schema_version=TEAM_IDENTITY_SCHEMA_VERSION,
        ),
    )


def _describe(exc: ValidationError) -> list[str]:
    """One phrase per error: the field and its error type, or this module's own message."""
    phrases: list[str] = []
    for err in exc.errors(
        include_input=False, include_context=False, include_url=False
    ):
        field = ".".join(str(part) for part in err["loc"]) or "<document>"
        if err["type"] == "value_error":
            # Raised by the declaration's own validator, which names positions, never values.
            phrases.append(f"{field}: {err['msg'].removeprefix('Value error, ')}")
        else:
            phrases.append(f"{field}: {err['type']}")
    return phrases


def check_cross_references(
    overlay: ModelTeamIdentityOverlay,
    *,
    developer_principals: Iterable[str] | None = None,
    lab_host_logins: Iterable[str] | None = None,
) -> None:
    """Every member principal is a developer principal; every operator principal a lab-host login.

    A ``None`` table is not checked. Raises :class:`TeamIdentityError` naming the entry position.
    """
    developers = (
        None if developer_principals is None else frozenset(developer_principals)
    )
    logins = None if lab_host_logins is None else frozenset(lab_host_logins)
    for index, entry in enumerate(overlay.entries):
        known = logins if entry.role == "operator" else developers
        if known is None:
            continue
        if not set(entry.ledger_principals) <= known:
            table = (
                "the dev-lane ACL lab-host logins"
                if entry.role == "operator"
                else "the developer principal overlay"
            )
            raise TeamIdentityError(
                f"{OVERLAY_ID}: entries.{index}.ledger_principals names a principal "
                f"absent from {table}"
            )


def _matches(entry: ModelTeamIdentityEntry, claim: ModelIdentityClaim) -> bool:
    value = getattr(entry, _CLAIM_FIELDS[claim.source])
    if isinstance(value, tuple):
        return claim.value in value
    return bool(value == claim.value)


def _team(
    diagnostic: str, source: ModelTeamIdentitySource | None
) -> ModelTeamIdentityResult:
    return ModelTeamIdentityResult(
        privileges="team",
        entry_id=None,
        role=None,
        diagnostic=diagnostic,
        source=source,
    )


def resolve_role(
    resolved: ModelResolvedTeamIdentity, claims: Sequence[ModelIdentityClaim]
) -> ModelTeamIdentityResult:
    """Resolve the claims to one entry; anything short of a clean match is team with a diagnostic."""
    source = resolved.source
    authenticated = [claim for claim in claims if claim.source in _CLAIM_FIELDS]
    if not authenticated:
        if claims:
            return _team(
                "only environment-supplied identity was offered; it is not positive "
                "identification, so team restrictions apply",
                source,
            )
        return _team("no identity is available; team restrictions apply", source)
    matched: set[str] = set()
    for claim in authenticated:
        entries = [
            entry for entry in resolved.overlay.entries if _matches(entry, claim)
        ]
        if not entries:
            return _team(
                f"the {claim.source} identity names no {OVERLAY_ID} entry; "
                "team restrictions apply",
                source,
            )
        matched.update(entry.id for entry in entries)
    if len(matched) != 1:
        return _team(
            f"the authenticated identities name {len(matched)} different {OVERLAY_ID} "
            "entries; team restrictions apply",
            source,
        )
    entry = next(entry for entry in resolved.overlay.entries if entry.id in matched)
    return ModelTeamIdentityResult(
        privileges="operator" if entry.role == "operator" else "team",
        entry_id=entry.id,
        role=entry.role,
        diagnostic=None,
        source=source,
    )


def broker_binding_claim() -> ModelIdentityClaim:
    """The broker login of the open authenticated lane bus, as a claim; never a secret.

    Valid only inside ``open_lab_run_bus(bus="kafka", ...)``; outside it the lane bus raises.
    """
    from omnimarket.delegated_test_loop.lane_bus import bound_lane_principal

    return ModelIdentityClaim(source="broker_binding", value=bound_lane_principal())


class HandlerTeamIdentity:
    def handle(self, request: ModelTeamIdentityRequest) -> ModelTeamIdentityResult:
        try:
            resolved = load_team_identity(request.overlay_roots)
        except TeamIdentityError as exc:
            return _team(f"{exc}; team restrictions apply", None)
        try:
            check_cross_references(
                resolved.overlay,
                developer_principals=request.developer_principals,
                lab_host_logins=request.lab_host_logins,
            )
        except TeamIdentityError as exc:
            return _team(f"{exc}; team restrictions apply", resolved.source)
        return resolve_role(resolved, request.claims)
