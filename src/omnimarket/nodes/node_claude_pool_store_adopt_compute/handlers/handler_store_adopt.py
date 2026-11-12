# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-23895: pure decision of which host-held labels to adopt into the secret store."""

from __future__ import annotations

from omnimarket.models.model_claude_pool_store_adopt import (
    ModelAdoptCommand,
    ModelAdoptDecision,
    ModelStoreAdoptRequest,
    ModelStoreAdoptResult,
)


class HandlerStoreAdopt:
    """Decides, per label held on any host, whether the store needs it, refuses it or already has it."""

    def handle(self, request: ModelStoreAdoptRequest) -> ModelStoreAdoptResult:
        if request.policy is None:
            return ModelStoreAdoptResult(configured=False)
        listed = {t.label: t for t in request.store_tokens}
        labels = sorted({row.label for row in request.held})
        decisions: list[ModelAdoptDecision] = []
        for label in labels:
            rows = [r for r in request.held if r.label == label]
            fingerprints = {r.fingerprint for r in rows}
            token = listed.get(label)
            if token is not None and token.fingerprint is not None:
                if token.fingerprint in fingerprints:
                    decisions.append(
                        ModelAdoptDecision(
                            label=label,
                            outcome="in_store",
                            reason="the store holds this label with a fingerprint a host also holds",
                        )
                    )
                else:
                    decisions.append(
                        ModelAdoptDecision(
                            label=label,
                            outcome="store_differs",
                            reason="the store holds a different value for this label than any host: never overwritten",
                        )
                    )
                continue
            if len(fingerprints) > 1:
                decisions.append(
                    ModelAdoptDecision(
                        label=label,
                        outcome="conflicting_fingerprints",
                        reason=f"hosts hold {len(fingerprints)} different values under this label: none is adopted",
                    )
                )
                continue
            live = sorted(r.host for r in rows if not r.revoked)
            if not live:
                decisions.append(
                    ModelAdoptDecision(
                        label=label,
                        outcome="revoked_everywhere",
                        reason="every host that holds this label has it revoked: a dead token is not adopted",
                    )
                )
                continue
            key = (
                token.key
                if token is not None
                else request.policy.key_prefix + label.upper().replace("-", "_")
            )
            decisions.append(
                ModelAdoptDecision(
                    label=label,
                    outcome="adopt",
                    reason=f"{live[0]} holds a live value the store does not: adopt it under {key}",
                    command=ModelAdoptCommand(
                        label=label,
                        key=key,
                        source_host=live[0],
                        fingerprint=sorted(fingerprints)[0],
                    ),
                )
            )
        return ModelStoreAdoptResult(configured=True, decisions=tuple(decisions))
