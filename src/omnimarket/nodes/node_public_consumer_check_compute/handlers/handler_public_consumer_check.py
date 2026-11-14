# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Public-consumer check: the gate every removal batch passes first (OMN-23145).

Rebuild plan section 5: each entity needs the public-consumer check first, a search of the public
repositories and published packages for imports and references, its evidence recorded; an unresolved
consumer keeps the entity. Rare paths (recovery, rollback, on-call) stay by declaration naming a drill.

An entity is kept when a public consumer has no recorded resolution, when a scope could not be read
(absence proves nothing), or when it declares a rare path that names a drill. A rare-path declaration
that names no drill exempts nothing. A batch that names any kept entity is refused as a whole.

A batch from an archived repository (OMN-23146) also names the repository's archived names. A public mention of one
of them with no recorded resolution keeps every entity of the batch, and the result records each mention and why.
"""

from __future__ import annotations

from omnimarket.nodes.node_public_consumer_check_compute.models.model_public_consumer_check import (
    EnumBatchDecision,
    EnumEntityDisposition,
    EnumScopeKind,
    ModelArchivedMention,
    ModelConsumerHit,
    ModelEntity,
    ModelEntityEvidence,
    ModelEntityVerdict,
    ModelPublicConsumerCheckRequest,
    ModelPublicConsumerCheckResult,
    ModelScopeReading,
    ModelSearchScope,
)
from omnimarket.nodes.node_public_consumer_check_compute.protocols.protocol_public_consumer_source import (
    ProtocolPublicConsumerSource,
    PublicConsumerSourceError,
)


class HandlerPublicConsumerCheck:
    """Pure over its injected source; no source and no scopes means every entity is kept."""

    def __init__(
        self,
        source: ProtocolPublicConsumerSource | None = None,
        scopes: tuple[ModelSearchScope, ...] = (),
    ) -> None:
        self._source = source
        self._scopes = scopes

    def handle(
        self, request: ModelPublicConsumerCheckRequest
    ) -> ModelPublicConsumerCheckResult:
        resolutions = {
            (r.scope.kind, r.scope.name, r.locator): r.resolution_ref
            for r in request.resolutions
        }
        mentions, unread = self._archived_mentions(request, resolutions)
        reasons = self._mention_reasons(request, mentions, unread)
        verdicts = tuple(
            self._with_mentions(
                self._verdict(request.repository, entity, resolutions),
                mentions,
                reasons,
                bool(unread),
            )
            for entity in request.entities
        )
        kept = tuple(v.entity_id for v in verdicts if v.kept)
        return ModelPublicConsumerCheckResult(
            batch_id=request.batch_id,
            repository=request.repository,
            decision=EnumBatchDecision.REFUSED if kept else EnumBatchDecision.ACCEPTED,
            kept_entity_ids=kept,
            verdicts=verdicts,
            archived_mentions=mentions,
            reasons=reasons,
        )

    def _archived_mentions(
        self,
        request: ModelPublicConsumerCheckRequest,
        resolutions: dict[tuple[EnumScopeKind, str, str], str],
    ) -> tuple[tuple[ModelArchivedMention, ...], tuple[str, ...]]:
        """The public mentions of the archived repository's names, and the scopes that could not be read for them."""
        mentions: list[ModelArchivedMention] = []
        unread: list[str] = []
        if not request.archived_repository_names:
            return (), ()
        for scope in self._scopes:
            if scope.name == request.repository:
                continue
            try:
                if self._source is None:
                    raise PublicConsumerSourceError("no search source is configured")
                for name in request.archived_repository_names:
                    for hit in self._source.search(scope, name):
                        ref = resolutions.get((scope.kind, scope.name, hit.locator), "")
                        mentions.append(
                            ModelArchivedMention(
                                scope=scope,
                                locator=hit.locator,
                                line=hit.line,
                                matched_name=hit.matched_reference,
                                resolved=bool(ref),
                                resolution_ref=ref,
                            )
                        )
            except PublicConsumerSourceError as exc:
                unread.append(f"{scope.kind.value}:{scope.name} unread: {exc}")
        return tuple(mentions), tuple(unread)

    @staticmethod
    def _mention_reasons(
        request: ModelPublicConsumerCheckRequest,
        mentions: tuple[ModelArchivedMention, ...],
        unread: tuple[str, ...],
    ) -> tuple[str, ...]:
        reasons = [
            f"archived repository {request.repository} is mentioned in "
            f"{m.scope.kind.value}:{m.scope.name}:{m.locator}:{m.line} as {m.matched_name}: "
            "its entities stay until the mention is resolved"
            for m in mentions
            if not m.resolved
        ]
        reasons.extend(
            f"search for the archived repository's names: scope {item}"
            for item in unread
        )
        return tuple(reasons)

    @staticmethod
    def _with_mentions(
        verdict: ModelEntityVerdict,
        mentions: tuple[ModelArchivedMention, ...],
        reasons: tuple[str, ...],
        mention_scope_unread: bool,
    ) -> ModelEntityVerdict:
        """Keep the entity for an unresolved mention of its archived repository, or for an unread mention search."""
        if not reasons:
            return verdict
        if any(not m.resolved for m in mentions):
            disposition = EnumEntityDisposition.KEPT_ARCHIVED_REPOSITORY_MENTION
        elif mention_scope_unread and not verdict.kept:
            disposition = EnumEntityDisposition.KEPT_SEARCH_UNREAD
        else:
            disposition = verdict.disposition
        return verdict.model_copy(
            update={
                "disposition": disposition,
                "kept": True,
                "reasons": (*reasons, *verdict.reasons),
            }
        )

    def _evidence(
        self,
        repository: str,
        entity: ModelEntity,
        resolutions: dict[tuple[EnumScopeKind, str, str], str],
    ) -> ModelEntityEvidence:
        readings: list[ModelScopeReading] = []
        hits: list[ModelConsumerHit] = []
        for scope in self._scopes:
            found: list[ModelConsumerHit] = []
            try:
                if self._source is None:
                    raise PublicConsumerSourceError("no search source is configured")
                for reference in entity.references:
                    for hit in self._source.search(scope, reference):
                        ref = resolutions.get((scope.kind, scope.name, hit.locator), "")
                        found.append(
                            ModelConsumerHit(
                                scope=scope,
                                locator=hit.locator,
                                line=hit.line,
                                matched_reference=hit.matched_reference,
                                own_repository=scope.name == repository,
                                resolved=bool(ref),
                                resolution_ref=ref,
                            )
                        )
            except PublicConsumerSourceError as exc:
                readings.append(
                    ModelScopeReading(
                        scope=scope, read=False, hit_count=0, unread_reason=str(exc)
                    )
                )
                continue
            readings.append(
                ModelScopeReading(scope=scope, read=True, hit_count=len(found))
            )
            hits.extend(found)
        return ModelEntityEvidence(scopes=tuple(readings), hits=tuple(hits))

    def _verdict(
        self,
        repository: str,
        entity: ModelEntity,
        resolutions: dict[tuple[EnumScopeKind, str, str], str],
    ) -> ModelEntityVerdict:
        evidence = self._evidence(repository, entity, resolutions)
        reasons: list[str] = []
        declared = entity.rare_path
        if declared is not None and declared.drill.strip():
            return ModelEntityVerdict(
                entity_id=entity.entity_id,
                disposition=EnumEntityDisposition.KEPT_RARE_PATH,
                kept=True,
                reasons=(
                    f"rare path declared ({declared.category.value}), drill {declared.drill.strip()}",
                ),
                evidence=evidence,
            )
        if declared is not None:
            reasons.append(
                f"rare path declared ({declared.category.value}) names no drill: not exempt"
            )
        unresolved = [
            h for h in evidence.hits if not h.resolved and not h.own_repository
        ]
        unread = [r for r in evidence.scopes if not r.read]
        if not self._scopes:
            reasons.append("no search scope is declared: nothing was searched")
        if unresolved:
            reasons.extend(
                f"unresolved consumer {h.scope.kind.value}:{h.scope.name}:{h.locator}:{h.line}"
                for h in unresolved
            )
            disposition = EnumEntityDisposition.KEPT_UNRESOLVED_CONSUMER
        elif unread or not self._scopes:
            reasons.extend(
                f"scope {r.scope.kind.value}:{r.scope.name} unread: {r.unread_reason}"
                for r in unread
            )
            disposition = EnumEntityDisposition.KEPT_SEARCH_UNREAD
        else:
            disposition = EnumEntityDisposition.CLEARED
        return ModelEntityVerdict(
            entity_id=entity.entity_id,
            disposition=disposition,
            kept=disposition is not EnumEntityDisposition.CLEARED,
            reasons=tuple(reasons),
            evidence=evidence,
        )
