# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The lab-model fallback for a unit no rule decided (OMN-22263).

Reachable only for the undetermined id. The route is local-only by the config's enum, its
answer is accepted only from a declared lab host, at the overlay's route model version and for
a declared id, and the stored result is written by one atomic put-if-absent.
"""

from __future__ import annotations

from omnimarket.models.work_classification.model_work_fallback import (
    EnumStoreStatus,
    ModelFallbackKey,
    ModelFallbackResult,
)
from omnimarket.nodes.node_work_intent_classify_effect.models import (
    EnumIntentStatus,
    ModelWorkIntentClassifyRequest,
    ModelWorkIntentClassifyResult,
)
from omnimarket.protocols.protocol_work_intent_route import (
    ProtocolFallbackResultStore,
    ProtocolWorkIntentRoute,
    RouteUnavailableError,
)


def _result(
    request: ModelWorkIntentClassifyRequest,
    status: EnumIntentStatus,
    *,
    work_type_id: str | None = None,
    cause: str | None = None,
    receipt: str | None = None,
    version: str | None = None,
    refused: bool = False,
) -> ModelWorkIntentClassifyResult:
    return ModelWorkIntentClassifyResult(
        unit_id=request.unit_id,
        status=status,
        work_type_id=work_type_id or request.undetermined_id,
        cause=cause,
        route_receipt_id=receipt,
        route_model_version=version,
        refused=refused,
    )


class HandlerWorkIntentClassify:
    """Effect: route and store-once ports injected at construction.

    A port left unwired is not replaced by an in-memory stand-in: the unit stays
    undetermined with the no-route cause, so the infra auto-wiring can build the
    handler with no ports and nothing is fabricated.
    """

    def __init__(
        self,
        *,
        route: ProtocolWorkIntentRoute | None = None,
        store: ProtocolFallbackResultStore | None = None,
    ) -> None:
        self._route = route
        self._store = store

    def handle(
        self, request: ModelWorkIntentClassifyRequest
    ) -> ModelWorkIntentClassifyResult:
        if request.current_work_type != request.undetermined_id:
            return _result(
                request,
                EnumIntentStatus.NOT_REACHED,
                work_type_id=request.current_work_type,
            )
        config = request.config
        if config is None:
            return _result(
                request, EnumIntentStatus.UNDETERMINED, cause=request.cause_no_route
            )
        if request.intent_text is None or request.intent_digest is None:
            return _result(
                request,
                EnumIntentStatus.UNDETERMINED,
                cause=request.cause_no_intent_text,
            )
        route = self._route
        store = self._store
        if route is None or store is None:
            return _result(
                request, EnumIntentStatus.UNDETERMINED, cause=request.cause_no_route
            )
        version = config.delegation_route.model_version
        key = ModelFallbackKey(
            intent_digest=request.intent_digest,
            rubric_version=request.rubric_version,
            route_model_version=version,
        )
        held = store.get(key)
        if held is not None:
            return _result(
                request,
                EnumIntentStatus.CACHED,
                work_type_id=held.work_type_id,
                receipt=held.route_receipt_id,
                version=version,
            )
        ids = tuple(
            i for i in request.allowed_work_type_ids if i != request.undetermined_id
        )
        prompt = request.prompt_template.format(
            ids=", ".join(ids), text=request.intent_text
        )
        try:
            answer = route.submit(
                prompt,
                policy=config.route_policy.value,
                hosts=config.lab_hosts,
                timeout_s=config.timeout_s,
            )
        except RouteUnavailableError:
            return _result(
                request, EnumIntentStatus.UNDETERMINED, cause=request.cause_no_route
            )
        if (
            answer.work_type_id not in ids
            or answer.host not in config.lab_hosts
            or answer.route_model_version != version
        ):
            return _result(
                request,
                EnumIntentStatus.UNDETERMINED,
                cause=request.cause_route_invalid,
            )
        outcome = store.put_if_absent(
            key,
            ModelFallbackResult(
                work_type_id=answer.work_type_id,
                route_receipt_id=answer.route_receipt_id,
            ),
        )
        return _result(
            request,
            EnumIntentStatus.DECIDED,
            work_type_id=outcome.stored.work_type_id,
            receipt=outcome.stored.route_receipt_id,
            version=version,
            refused=outcome.status is EnumStoreStatus.REFUSED,
        )


__all__ = ["HandlerWorkIntentClassify"]
