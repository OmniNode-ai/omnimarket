# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Pure grant-resolution logic for the Phase-2b resolver EFFECT (OMN-13439).

Parses the ``onex_change_control`` ``grants/prod_promotion_grants.yaml`` trust
anchor (the OMN-13437 schema) and resolves it against one redeploy request key
``(promotion_batch_id, image_digest, lane=prod)``. This module does ZERO I/O — it
takes the already-fetched file bytes + a deterministic ``evaluated_at`` and
returns a typed resolution. The fetch (from ``onex_change_control@main``) lives in
the handler's I/O boundary.

onex_change_control keeps ZERO Python import on omnimarket: the grant file is the
contract surface and this resolver parses its YAML directly — it never imports a
validator or model from the governance repo.

Match + lifecycle rules (DoD):
  * lane must equal ``prod`` and digest + batch must match the request key;
  * a ``consumed: true`` (or removed) entry resolves to ABSENT — no replay;
  * ``evaluated_at > expires_at`` resolves to EXPIRED;
  * ``approved_by == requested_by`` resolves to SELF_GRANTED (rejected);
  * a present, future-expiry, matching, non-self entry resolves to RESOLVED and
    materializes ``ModelProdPromotionGrant``.

Every non-RESOLVED outcome leaves the grant ``None`` so the prod gate fails
closed. There is no silent default.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any
from uuid import UUID

import yaml
from pydantic import BaseModel, ConfigDict

from omnimarket.events.runtime_deployment import (
    EnumGrantResolution,
    EnumRuntimeLane,
    ModelProdPromotionGrant,
)

# Required fields per the OMN-13437 grant-file schema. ``consumed``, ``consumed_at``
# and ``consumed_by_correlation_id`` are OPTIONAL lifecycle markers (OMN-13424;
# absent == not consumed); every other field is mandatory.
_REQUIRED_ENTRY_FIELDS: frozenset[str] = frozenset(
    {
        "grant_id",
        "runtime_lane",
        "image_digest",
        "promotion_batch_id",
        "approved_by",
        "expires_at",
        "created_at",
        "reason",
    }
)

_PROD_LANE_VALUE = EnumRuntimeLane.PROD.value


class ModelGrantResolution(BaseModel):
    """Resolver output: typed outcome + materialized grant + matched grant_id.

    ``grant`` is populated only when ``outcome is RESOLVED``; ``grant_id`` carries
    the matched entry's id even for EXPIRED / CONSUMED / SELF_GRANTED so the audit
    provenance can name the rejected entry. ABSENT leaves both ``None``.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    outcome: EnumGrantResolution
    grant: ModelProdPromotionGrant | None = None
    grant_id: str | None = None


def file_sha256(raw: bytes) -> str:
    """Return the sha256 hex digest of the exact grant-file bytes."""
    return hashlib.sha256(raw).hexdigest()


def _coerce_datetime(value: Any) -> datetime:
    """Coerce a grant-file timestamp (ISO-8601, ``Z`` allowed) to ``datetime``.

    The schema validator (OMN-13437) already enforces ISO-8601 UTC at landing, so
    a parse failure here means the anchor is corrupt — fail fast rather than
    silently treat the grant as absent.
    """
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    raise TypeError(
        f"grant timestamp must be a datetime or ISO-8601 string; got {value!r}"
    )


def _is_consumed(entry: Mapping[str, Any]) -> bool:
    """Whether an entry is marked consumed. Absent marker == not consumed."""
    return entry.get("consumed", False) is True


def _matches_key(
    entry: Mapping[str, Any],
    *,
    requested_image_digest: str | None,
    promotion_batch_id: str | None,
) -> bool:
    """Whether the entry matches the request key (lane=prod, digest, batch)."""
    if str(entry.get("runtime_lane")) != _PROD_LANE_VALUE:
        return False
    if requested_image_digest is None or promotion_batch_id is None:
        return False
    return (
        str(entry.get("image_digest")) == requested_image_digest
        and str(entry.get("promotion_batch_id")) == promotion_batch_id
    )


def _materialize_grant(entry: Mapping[str, Any]) -> ModelProdPromotionGrant:
    """Materialize a matched entry into the canonical grant DTO.

    Maps the OMN-13437 file field names onto the ``ModelProdPromotionGrant``
    field names (``runtime_lane`` -> ``approved_lane`` etc.). The optional
    single-use lifecycle markers (``consumed_at``, ``consumed_by_correlation_id``,
    OMN-13424) carry through when present; absent markers stay ``None``.
    """
    consumed_at_raw = entry.get("consumed_at")
    consumed_corr_raw = entry.get("consumed_by_correlation_id")
    return ModelProdPromotionGrant(
        grant_id=str(entry["grant_id"]),
        approved_lane=EnumRuntimeLane(str(entry["runtime_lane"])),
        approved_image_digest=str(entry["image_digest"]),
        approved_promotion_batch_id=str(entry["promotion_batch_id"]),
        approved_by=str(entry["approved_by"]),
        created_at=_coerce_datetime(entry["created_at"]),
        expires_at=_coerce_datetime(entry["expires_at"]),
        consumed_at=(
            _coerce_datetime(consumed_at_raw) if consumed_at_raw is not None else None
        ),
        consumed_by_correlation_id=(
            UUID(str(consumed_corr_raw)) if consumed_corr_raw is not None else None
        ),
    )


def _parse_entries(raw: bytes) -> Sequence[Mapping[str, Any]]:
    """Parse the grant file bytes into the list of entry mappings.

    A malformed anchor (non-mapping top level, missing ``entries`` key, or an
    entry missing a required field) raises — the resolver must not silently treat
    a corrupt trust anchor as "no grant".
    """
    data = yaml.safe_load(raw.decode("utf-8")) if raw.strip() else None
    if data is None:
        return ()
    if not isinstance(data, Mapping):
        raise ValueError("grant file must be a YAML mapping at the top level")
    entries = data.get("entries")
    if entries is None:
        raise ValueError("grant file missing top-level 'entries' key")
    if not isinstance(entries, list):
        raise ValueError("grant file 'entries' must be a list")
    parsed: list[Mapping[str, Any]] = []
    for index, entry in enumerate(entries):
        if not isinstance(entry, Mapping):
            raise ValueError(f"grant entry #{index} must be a mapping")
        missing = _REQUIRED_ENTRY_FIELDS - set(entry.keys())
        if missing:
            raise ValueError(
                f"grant entry #{index} missing required fields: {sorted(missing)}"
            )
        parsed.append(entry)
    return tuple(parsed)


def resolve_grant(
    raw: bytes,
    *,
    requested_image_digest: str | None,
    promotion_batch_id: str | None,
    requested_by: str,
    evaluated_at: datetime,
) -> ModelGrantResolution:
    """Resolve the grant file bytes against one request key.

    Pure + deterministic: identical inputs always yield the same resolution. The
    first matching entry decides the outcome (the schema makes grant_id unique, so
    at most one entry matches a digest+batch key in practice).
    """
    entries = _parse_entries(raw)
    for entry in entries:
        if not _matches_key(
            entry,
            requested_image_digest=requested_image_digest,
            promotion_batch_id=promotion_batch_id,
        ):
            continue

        grant_id = str(entry["grant_id"])

        # Consumed grants are NOT replayed: a single-use approval that already
        # promoted is treated as absent so a second promotion needs a fresh grant.
        if _is_consumed(entry):
            return ModelGrantResolution(
                outcome=EnumGrantResolution.CONSUMED, grant_id=grant_id
            )

        # Anti-self-grant: the approver must differ from the requester.
        if str(entry["approved_by"]) == requested_by:
            return ModelGrantResolution(
                outcome=EnumGrantResolution.SELF_GRANTED, grant_id=grant_id
            )

        # Absolute expiry (inclusive boundary): evaluated_at must not exceed it.
        if evaluated_at > _coerce_datetime(entry["expires_at"]):
            return ModelGrantResolution(
                outcome=EnumGrantResolution.EXPIRED, grant_id=grant_id
            )

        return ModelGrantResolution(
            outcome=EnumGrantResolution.RESOLVED,
            grant=_materialize_grant(entry),
            grant_id=grant_id,
        )

    return ModelGrantResolution(outcome=EnumGrantResolution.ABSENT)


class ModelPruneResult(BaseModel):
    """Result of pruning expired/consumed entries from the grant anchor.

    ``raw`` is the re-serialized grant file with only the still-live entries kept
    (single-key ``entries`` mapping). ``pruned_grant_ids`` names every dropped
    entry so the prune action carries audit provenance. ``had_expired`` is the
    lint signal: a CI prune job FAILs when ``True``, since at-rest state requires
    ``entries: []`` of expired/consumed grants.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    raw: bytes
    pruned_grant_ids: tuple[str, ...] = ()
    had_expired: bool = False


def _is_entry_expired(entry: Mapping[str, Any], *, evaluated_at: datetime) -> bool:
    """Whether an entry is past its absolute expiry at ``evaluated_at``.

    Inclusive boundary matches ``resolve_grant``: an entry is expired only when
    ``evaluated_at`` strictly exceeds ``expires_at``.
    """
    return evaluated_at > _coerce_datetime(entry["expires_at"])


def prune_expired(raw: bytes, *, evaluated_at: datetime) -> ModelPruneResult:
    """Drop expired and consumed entries from the grant anchor, deterministically.

    A grant is single-use and time-bound (OMN-13424): once it is consumed by a
    terminal promotion or its absolute ``expires_at`` has passed, it must not
    linger in the trust anchor where it could be replayed or accumulate. This pure
    function re-serializes the file keeping only still-live, unconsumed entries and
    reports which grant_ids were dropped.

    Pure + deterministic: it performs ZERO I/O. The fetch + write-back PR lives in
    the handler / CI boundary. A corrupt anchor raises (via ``_parse_entries``)
    rather than silently pruning everything.
    """
    entries = _parse_entries(raw)
    kept: list[Mapping[str, Any]] = []
    pruned: list[str] = []
    had_expired = False
    for entry in entries:
        expired = _is_entry_expired(entry, evaluated_at=evaluated_at)
        if expired:
            had_expired = True
        if expired or _is_consumed(entry):
            pruned.append(str(entry["grant_id"]))
            continue
        kept.append(entry)
    serialized = yaml.safe_dump(
        {"entries": [dict(entry) for entry in kept]},
        sort_keys=False,
    ).encode("utf-8")
    return ModelPruneResult(
        raw=serialized,
        pruned_grant_ids=tuple(pruned),
        had_expired=had_expired,
    )


__all__: list[str] = [
    "ModelGrantResolution",
    "ModelPruneResult",
    "file_sha256",
    "prune_expired",
    "resolve_grant",
]
