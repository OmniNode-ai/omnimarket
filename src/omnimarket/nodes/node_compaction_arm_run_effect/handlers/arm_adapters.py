# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Native headless compaction and the pinned context-pack bus adapter."""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from contextlib import ExitStack
from datetime import UTC, datetime
from typing import Protocol
from uuid import uuid4

from omnibase_core.enums.enum_context_factor import EnumContextFactor
from omnibase_core.models.context.model_context_access import ModelContextAccess
from omnibase_core.models.context.model_context_caller_scope import (
    ModelContextCallerScope,
)
from omnibase_core.models.events.model_event_envelope import ModelEventEnvelope

from omnimarket.models.compaction_replay.model_native_compaction_fork import (
    ModelNativeCompactionForkRequest,
    ModelNativeCompactionForkResult,
)
from omnimarket.models.context_pack_orchestrator import (
    EnumContextPackMode,
    ModelContextPackBuilt,
    ModelContextPackCommand,
    ModelContextPackRequest,
)
from omnimarket.nodes.node_compaction_arm_run_effect.handlers.workspace import (
    ModelArmWorkspace,
)
from omnimarket.nodes.node_compaction_arm_run_effect.models.model_arm_run import (
    ModelArmRunRequest,
    ModelCompactionObservation,
    ModelUsage,
)
from omnimarket.nodes.node_compaction_arm_run_effect.protocols.protocol_arm_adapter import (
    ProtocolArmAdapter,
)


class ProtocolTerminal(Protocol):
    def register(self, correlation_id: str) -> None: ...
    def wait(
        self, correlation_id: str, timeout_seconds: float
    ) -> dict[str, object] | None: ...
    def close(self) -> None: ...


class ProtocolTerminalConsumer(Protocol):
    def open(self, topic: str) -> ProtocolTerminal: ...


class HandlerNativeForkAdapter:
    """Request native compaction from the sanctioned delegation node over the bus."""

    def __init__(
        self,
        publisher: Callable[[str, bytes], None],
        consumer: ProtocolTerminalConsumer,
        *,
        requested_topic: str,
        completed_topic: str,
        timeout_s: float,
    ) -> None:
        self._publisher, self._consumer = publisher, consumer
        self._requested, self._completed, self._timeout = (
            requested_topic,
            completed_topic,
            timeout_s,
        )

    def compact(
        self, request: ModelArmRunRequest, workspace: ModelArmWorkspace
    ) -> ModelCompactionObservation:
        cid = uuid4()
        command = ModelNativeCompactionForkRequest(
            case=request.case, transcript_prefix=request.transcript_prefix
        )
        with ExitStack() as stack:
            terminal = self._consumer.open(self._completed)
            stack.callback(terminal.close)
            terminal.register(str(cid))
            self._publisher(
                self._requested,
                ModelEventEnvelope[ModelNativeCompactionForkRequest](
                    payload=command,
                    correlation_id=cid,
                    event_type=self._requested,
                )
                .model_dump_json()
                .encode(),
            )
            reply = terminal.wait(str(cid), self._timeout)
        if reply is None:
            return ModelCompactionObservation(
                reply="unavailable", evidence="native compaction bus unavailable"
            )
        if str(reply.get("correlation_id")) != str(cid):
            raise ValueError("native compaction correlation differs from request")
        return ModelNativeCompactionForkResult.model_validate(reply.get("payload"))


class HandlerPinnedPackAdapter:
    """Use the existing pack orchestrator via its restricted command topics."""

    def __init__(
        self,
        publisher: Callable[[str, bytes], None],
        consumer: ProtocolTerminalConsumer,
        *,
        access: ModelContextAccess,
        profile_name: str,
        requested_topic: str,
        built_topic: str,
        failed_topic: str,
        timeout_s: float,
    ) -> None:
        self._publisher, self._consumer, self._access = publisher, consumer, access
        self._profile = profile_name
        self._requested, self._built, self._failed = (
            requested_topic,
            built_topic,
            failed_topic,
        )
        self._timeout = timeout_s

    def compact(
        self, request: ModelArmRunRequest, workspace: ModelArmWorkspace
    ) -> ModelCompactionObservation:
        if (
            request.case.boundary_at is None
            or len(request.case.lane_attribution.lanes) != 1
        ):
            return ModelCompactionObservation(
                reply="refused",
                evidence="pinned pack requires a boundary instant and one attributed lane",
            )
        moment = datetime.fromisoformat(request.case.boundary_at)
        if moment.tzinfo is None:
            return ModelCompactionObservation(
                reply="refused",
                evidence="pinned pack requires a timezone-aware boundary instant",
            )
        revision = moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        command = ModelContextPackCommand(
            access=self._access,
            request=ModelContextPackRequest(
                profile_name=self._profile,
                mode=EnumContextPackMode.PINNED,
                manifest_case=request.case.case_key,
                session_id=request.case.session_id,
                caller_scope=ModelContextCallerScope(
                    lane=request.case.lane_attribution.lanes[0]
                ),
                tier_revisions={
                    EnumContextFactor.OPERATOR_RULINGS: revision,
                    EnumContextFactor.OPEN_WORK: revision,
                    EnumContextFactor.ORCHESTRATION_STATE: revision,
                    EnumContextFactor.OPERATOR_WORDS_UNRECORDED: request.case.prefix_sha256,
                    EnumContextFactor.REPO_STATE: request.case.prefix_sha256,
                },
            ),
        )
        cid = uuid4()
        started = time.monotonic()
        command_path = workspace.root / "pack-command.json"
        reply_path = workspace.root / "pack-reply.json"
        command_path.write_text(command.model_dump_json(indent=2), encoding="utf-8")
        with ExitStack() as stack:
            built = self._consumer.open(self._built)
            stack.callback(built.close)
            failed = self._consumer.open(self._failed)
            stack.callback(failed.close)
            built.register(str(cid))
            failed.register(str(cid))
            self._publisher(
                self._requested,
                ModelEventEnvelope[ModelContextPackCommand](
                    payload=command,
                    correlation_id=cid,
                    event_type=self._requested,
                )
                .model_dump_json()
                .encode(),
            )
            reply = built.wait(str(cid), self._timeout)
        reply_path.write_text(json.dumps(reply, sort_keys=True), encoding="utf-8")
        artifacts = (str(command_path), str(reply_path))
        elapsed = (time.monotonic() - started) * 1000
        if reply is None:
            return ModelCompactionObservation(
                reply="native",
                evidence="pack build unavailable",
                usage=ModelUsage(wall_ms=elapsed),
                adapter_artifacts=artifacts,
            )
        if str(reply.get("correlation_id")) != str(cid):
            raise ValueError("pack reply correlation differs from request")
        result = ModelContextPackBuilt.model_validate(reply.get("payload"))
        if (
            result.status != "ok"
            or result.profile_name != self._profile
            or not result.messages
            or not all(
                (
                    result.candidate_id,
                    result.assembly_hash,
                    result.final_pack_hash,
                    result.body_hash,
                    result.version_vector_hash,
                    result.profile_content_hash,
                )
            )
        ):
            return ModelCompactionObservation(
                reply="native",
                evidence="pinned pack build failed",
                usage=ModelUsage(wall_ms=elapsed),
                adapter_artifacts=artifacts,
            )
        return ModelCompactionObservation(
            reply="installed",
            context="\n\n".join(message.text for message in result.messages),
            package_hash=result.final_pack_hash,
            evidence="pinned context-pack build",
            # Factor estimates are not request usage. No measured token count is
            # invented from them. Wall time alone is measured here.
            usage=ModelUsage(wall_ms=elapsed),
            adapter_artifacts=artifacts,
        )


class HandlerModAdapter:
    """A native reply remains in the mod denominator and captures native context."""

    def __init__(self, pack: ProtocolArmAdapter, native: ProtocolArmAdapter) -> None:
        self._pack, self._native = pack, native

    def compact(
        self, request: ModelArmRunRequest, workspace: ModelArmWorkspace
    ) -> ModelCompactionObservation:
        try:
            packed = self._pack.compact(request, workspace)
        except Exception as exc:
            packed = ModelCompactionObservation(
                reply="native", evidence=f"pinned pack error: {type(exc).__name__}"
            )
        if packed.reply != "native":
            return packed
        native = self._native.compact(request, workspace)
        return native.model_copy(
            update={
                "reply": "native",
                "evidence": packed.evidence + "; " + native.evidence,
                "adapter_artifacts": (
                    *packed.adapter_artifacts,
                    *native.adapter_artifacts,
                ),
            }
        )


class HandlerUnavailableArmAdapter:
    """Arm adapter bound when the pack bus ports or tenant are not configured."""

    def compact(
        self, request: ModelArmRunRequest, workspace: ModelArmWorkspace
    ) -> ModelCompactionObservation:
        return ModelCompactionObservation(
            reply="unavailable", evidence="pack bus ports or tenant are not configured"
        )
