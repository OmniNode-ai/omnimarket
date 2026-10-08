# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Pure definition-B evaluation sampler with caller-resolved sampling policy."""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict

from omnimarket.nodes.node_delegation_eval_sample_compute.models.enum_delegation_eval_gate_outcome import (
    EnumDelegationEvalGateOutcome,
)
from omnimarket.nodes.node_delegation_eval_sample_compute.models.enum_delegation_eval_import_rejection import (
    EnumDelegationEvalImportRejection,
)
from omnimarket.nodes.node_delegation_eval_sample_compute.models.enum_delegation_eval_source import (
    EnumDelegationEvalSource,
)
from omnimarket.nodes.node_delegation_eval_sample_compute.models.model_delegation_eval_candidate import (
    ModelDelegationEvalCandidate,
)
from omnimarket.nodes.node_delegation_eval_sample_compute.models.model_delegation_eval_item import (
    ModelDelegationEvalItem,
)
from omnimarket.nodes.node_delegation_eval_sample_compute.models.model_delegation_eval_key import (
    ModelDelegationEvalKey,
)
from omnimarket.nodes.node_delegation_eval_sample_compute.models.model_delegation_eval_manifest import (
    ModelDelegationEvalManifest,
)
from omnimarket.nodes.node_delegation_eval_sample_compute.models.model_delegation_eval_rejected_import import (
    ModelDelegationEvalRejectedImport,
)
from omnimarket.nodes.node_delegation_eval_sample_compute.models.model_delegation_eval_sample_request import (
    ModelDelegationEvalSampleRequest,
)
from omnimarket.nodes.node_delegation_eval_sample_compute.models.model_delegation_eval_shortfall import (
    ModelDelegationEvalShortfall,
)


def _key(candidate: ModelDelegationEvalCandidate) -> ModelDelegationEvalKey:
    return ModelDelegationEvalKey(
        correlation_id=candidate.correlation_id, attempt_index=candidate.attempt_index
    )


def _order(seed: str, key: ModelDelegationEvalKey) -> tuple[str, str, int]:
    return (
        hashlib.sha256(
            f"{seed}{key.correlation_id}{key.attempt_index}".encode()
        ).hexdigest(),
        key.correlation_id,
        key.attempt_index,
    )


def _stratum(candidate: ModelDelegationEvalCandidate) -> str:
    stratum = f"{candidate.task_class}/{candidate.gate_outcome}"
    if candidate.task_class == "code_generation":
        stratum += f"/{candidate.deciding_path or 'none'}"
    return stratum


class HandlerDelegationEvalSample:
    """Select imports before draws, with stable ordering and no handler I/O.

    Exact duplicate rows count once. Conflicting metadata for an identity is
    rejected rather than allowing input order to choose its eligibility.
    When imports exceed a quota, their seeded ordering determines the subset.
    """

    def handle(
        self, request: ModelDelegationEvalSampleRequest
    ) -> ModelDelegationEvalManifest:
        config = request.sampling
        ordered_quotas = tuple(sorted(config.quotas, key=lambda row: row.name))
        quotas = {row.name: row.count for row in ordered_quotas}
        candidates: dict[ModelDelegationEvalKey, ModelDelegationEvalCandidate] = {}
        for candidate in request.candidates:
            key = _key(candidate)
            if key in candidates and candidates[key] != candidate:
                raise ValueError(f"Conflicting candidate metadata for {key}")
            candidates[key] = candidate

        excluded: dict[ModelDelegationEvalKey, EnumDelegationEvalImportRejection] = {}
        groups: dict[
            tuple[str, EnumDelegationEvalGateOutcome, str],
            list[ModelDelegationEvalCandidate],
        ] = defaultdict(list)
        for key, candidate in candidates.items():
            # Tenant exclusion precedes holdout exclusion, including for imports.
            if candidate.tenant_id != request.house_tenant_id:
                excluded[key] = EnumDelegationEvalImportRejection.CUSTOMER_TENANT
            elif (
                int(hashlib.sha256(key.correlation_id.encode()).hexdigest(), 16)
                % config.holdout_buckets
                == config.reserved_bucket
            ):
                excluded[key] = EnumDelegationEvalImportRejection.HOLDOUT_BUCKET
            else:
                # Display labels can collide when class or path contains '/'.
                # Keep the fields separate so row order cannot choose a quota.
                group = (
                    candidate.task_class,
                    candidate.gate_outcome,
                    (candidate.deciding_path or "none")
                    if candidate.task_class == "code_generation"
                    else "",
                )
                groups[group].append(candidate)

        imports = set(request.imported_keys)
        rejected = tuple(
            ModelDelegationEvalRejectedImport(
                key=key,
                reason=excluded.get(key, EnumDelegationEvalImportRejection.UNKNOWN_KEY),
            )
            for key in sorted(
                imports, key=lambda key: (key.correlation_id, key.attempt_index)
            )
            if key in excluded or key not in candidates
        )
        items: list[ModelDelegationEvalItem] = []
        shortfalls: list[ModelDelegationEvalShortfall] = []
        for (_, outcome, _), rows in sorted(groups.items()):
            stratum = _stratum(rows[0])
            # quotas is keyed by gate outcome (EnumDelegationEvalGateOutcome),
            # which is the middle element of the group key, not task_class.
            quota = quotas[outcome]
            ordered = sorted(rows, key=lambda row: _order(request.seed, _key(row)))
            imported = [row for row in ordered if _key(row) in imports]
            drawn = [row for row in ordered if _key(row) not in imports]
            selected = (imported + drawn)[:quota]
            items.extend(
                ModelDelegationEvalItem(
                    key=_key(row),
                    task_class=row.task_class,
                    stratum=stratum,
                    source=EnumDelegationEvalSource.IMPORTED
                    if _key(row) in imports
                    else EnumDelegationEvalSource.DRAWN,
                )
                for row in selected
            )
            if len(rows) < quota:
                shortfalls.append(
                    ModelDelegationEvalShortfall(
                        stratum=stratum,
                        quota=quota,
                        available=len(rows),
                        taken=len(selected),
                    )
                )

        items.sort(key=lambda item: (item.stratum, *_order(request.seed, item.key)))
        # Sorted mapping keys, compact separators, and the already canonical row
        # ordering define the exact manifest identity serialization.
        identity = {
            "seed": request.seed,
            "window_start": request.window_start,
            "window_end": request.window_end,
            "query_text": request.query_text,
            "quotas": [row.model_dump(mode="json") for row in ordered_quotas],
            "item_keys": [item.key.model_dump(mode="json") for item in items],
        }
        manifest_id = hashlib.sha256(
            json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        return ModelDelegationEvalManifest(
            manifest_id=manifest_id,
            seed=request.seed,
            window_start=request.window_start,
            window_end=request.window_end,
            query_text=request.query_text,
            quotas=ordered_quotas,
            items=tuple(items),
            shortfalls=tuple(shortfalls),
            excluded_holdout_bucket=sum(
                reason == EnumDelegationEvalImportRejection.HOLDOUT_BUCKET
                for reason in excluded.values()
            ),
            excluded_customer_tenant=sum(
                reason == EnumDelegationEvalImportRejection.CUSTOMER_TENANT
                for reason in excluded.values()
            ),
            rejected_imports=rejected,
        )


__all__ = ["HandlerDelegationEvalSample"]
