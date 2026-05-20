# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Contract registry handler — validates and stores dynamic contract registrations."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, Protocol

import yaml

from omnimarket.nodes.contract_topics import contract_publish_topics
from omnimarket.nodes.node_contract_registry.models.enums import (
    EnumMaterializationRejection,
    EnumMaterializationStatus,
)
from omnimarket.nodes.node_contract_registry.models.models import (
    ModelContractRegistrationRequest,
    ModelContractRegistrationResult,
)

ALLOWED_HANDLER_PREFIXES: tuple[str, ...] = (
    "omnibase_infra.handlers.",
    "omniintelligence.nodes.",
    "omnimarket.nodes.",
)

_CONTRACT_PATH = Path(__file__).parent.parent / "contract.yaml"
_PUBLISH_TOPICS = contract_publish_topics(_CONTRACT_PATH)
# publish_topics order: node-registration.v1 (index 0), node-registration-rejected.v1 (index 1)
TOPIC_REGISTRATION_VALIDATED = _PUBLISH_TOPICS[0]
TOPIC_REGISTRATION_REJECTED = _PUBLISH_TOPICS[1]


def _read_active_profiles() -> tuple[str, ...]:
    raw = yaml.safe_load(_CONTRACT_PATH.read_text(encoding="utf-8"))
    profiles = raw.get("runtime_profiles", [])
    return tuple(profiles) if isinstance(profiles, list) else ()


class EventPublisher(Protocol):
    def publish(self, topic: str, payload: dict[str, Any]) -> None: ...


class ContractRegistryHandler:
    """Validate, store, and publish dynamic contract registration requests."""

    def __init__(
        self,
        publisher: EventPublisher | None = None,
        active_profiles: tuple[str, ...] | None = None,
    ) -> None:
        self._publisher = publisher
        self._active_profiles = (
            active_profiles if active_profiles is not None else _read_active_profiles()
        )
        self._registry: dict[str, str] = {}

    def handle(
        self, request: ModelContractRegistrationRequest
    ) -> ModelContractRegistrationResult:
        # Step 1: parse YAML
        try:
            parsed: Any = yaml.safe_load(request.contract_yaml)
            if not isinstance(parsed, dict):
                raise ValueError("contract_yaml must be a YAML mapping")
        except Exception:
            return self._reject(
                request,
                EnumMaterializationRejection.PARSE_FAILURE,
            )

        # Step 2: verify hash
        recomputed = hashlib.sha256(request.contract_yaml.encode()).hexdigest()
        if recomputed != request.contract_hash:
            return self._reject(request, EnumMaterializationRejection.HASH_MISMATCH)

        # Step 3: handler allowlist
        handler_routing = parsed.get("handler_routing", {})
        handlers = handler_routing.get("handlers", [])
        for entry in handlers:
            handler = entry.get("handler", {}) if isinstance(entry, dict) else {}
            module = handler.get("module", "") or entry.get("handler_module", "")
            if module and not any(
                module.startswith(p) for p in ALLOWED_HANDLER_PREFIXES
            ):
                return self._reject(
                    request, EnumMaterializationRejection.HANDLER_ALLOWLIST
                )

        # Step 4: runtime profiles
        contract_profiles = parsed.get("runtime_profiles", [])
        if (
            isinstance(contract_profiles, list)
            and contract_profiles
            and not any(p in self._active_profiles for p in contract_profiles)
        ):
            return self._reject(request, EnumMaterializationRejection.PROFILE_MISMATCH)

        # Step 5: version conflict
        existing_hash = self._registry.get(request.node_name)
        if existing_hash is not None:
            if existing_hash != request.contract_hash:
                return self._reject(
                    request, EnumMaterializationRejection.VERSION_CONFLICT
                )
            # idempotent: same name, same hash
            return ModelContractRegistrationResult(
                node_name=request.node_name,
                contract_hash=request.contract_hash,
                correlation_id=request.correlation_id,
                status=EnumMaterializationStatus.ALREADY_MATERIALIZED,
                stored=True,
                published_topic="",
                mcp_eligible=False,
                mcp_tags=(),
            )

        # Step 6: store
        self._registry[request.node_name] = request.contract_hash

        # Step 7 & 8: build MCP tags and publish
        mcp_meta = parsed.get("metadata", {}) or {}
        mcp_eligible = bool(mcp_meta.get("mcp_enabled", False))
        node_type = parsed.get("node_type", "unknown")
        mcp_tags: tuple[str, ...] = (
            f"node-type:{node_type}",
            f"mcp-tool:{request.node_name}",
        )
        if mcp_eligible:
            mcp_tags = ("mcp-enabled", *mcp_tags)

        publish_topic = TOPIC_REGISTRATION_VALIDATED
        result = ModelContractRegistrationResult(
            node_name=request.node_name,
            contract_hash=request.contract_hash,
            correlation_id=request.correlation_id,
            status=EnumMaterializationStatus.MATERIALIZED,
            stored=True,
            published_topic=publish_topic,
            mcp_eligible=mcp_eligible,
            mcp_tags=mcp_tags,
        )
        if self._publisher is not None:
            self._publisher.publish(publish_topic, result.model_dump(mode="json"))
        return result

    def _reject(
        self,
        request: ModelContractRegistrationRequest,
        reason: EnumMaterializationRejection,
    ) -> ModelContractRegistrationResult:
        reject_topic = TOPIC_REGISTRATION_REJECTED
        result = ModelContractRegistrationResult(
            node_name=request.node_name,
            contract_hash=request.contract_hash,
            correlation_id=request.correlation_id,
            status=EnumMaterializationStatus.REJECTED,
            reason=reason,
            stored=False,
            published_topic=reject_topic,
        )
        if self._publisher is not None:
            self._publisher.publish(reject_topic, result.model_dump(mode="json"))
        return result


__all__ = ["ALLOWED_HANDLER_PREFIXES", "ContractRegistryHandler"]
