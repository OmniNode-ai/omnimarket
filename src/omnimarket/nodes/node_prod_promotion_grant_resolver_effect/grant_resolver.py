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

import yaml
from pydantic import BaseModel, ConfigDict

from omnimarket.events.runtime_deployment import (
    EnumGrantResolution,
    EnumRuntimeLane,
    ModelProdPromotionGrant,
)

# Required fields per the OMN-13437 grant-file schema. ``consumed`` is an OPTIONAL
# lifecycle marker (absent == not consumed); every other field is mandatory.
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
    field names (``runtime_lane`` -> ``approved_lane`` etc.).
    """
    return ModelProdPromotionGrant(
        grant_id=str(entry["grant_id"]),
        approved_lane=EnumRuntimeLane(str(entry["runtime_lane"])),
        approved_image_digest=str(entry["image_digest"]),
        approved_promotion_batch_id=str(entry["promotion_batch_id"]),
        approved_by=str(entry["approved_by"]),
        created_at=_coerce_datetime(entry["created_at"]),
        expires_at=_coerce_datetime(entry["expires_at"]),
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


__all__: list[str] = [
    "ModelGrantResolution",
    "file_sha256",
    "resolve_grant",
]
