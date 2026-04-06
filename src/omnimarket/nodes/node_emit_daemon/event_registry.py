# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Pluggable event registry loaded from per-platform YAML files.

The event registry defines how events are routed to Kafka topics with
fan-out support: a single event type can be published to multiple topics
with different payload transformations.

Key design decisions:
    - YAML per platform (claude_code.yaml, cursor.yaml, etc.)
    - Symbolic transform names mapped to fixed callables (no dynamic imports)
    - EventRegistration and FanOutRule are portable dataclasses
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import yaml

logger = logging.getLogger(__name__)

# Type alias for payload transform functions
PayloadTransform = Callable[[dict[str, object]], dict[str, object]]


# =============================================================================
# Built-in Transform Functions
# =============================================================================


def transform_passthrough(payload: dict[str, object]) -> dict[str, object]:
    """Passthrough transform -- returns payload unchanged."""
    return payload


def transform_strip_prompt(payload: dict[str, object]) -> dict[str, object]:
    """Strip full prompt content, keeping only preview and length.

    Suitable for observability topics where full prompt must not appear.
    """
    result: dict[str, object] = dict(payload)

    # Strip base64-encoded prompt
    result.pop("prompt_b64", None)
    result.pop("prompt", None)

    # Ensure preview exists and is truncated
    preview = payload.get("prompt_preview", "")
    if not isinstance(preview, str):
        preview = str(preview) if preview is not None else ""
    result["prompt_preview"] = preview[:100]

    # Record length if not present
    if "prompt_length" not in result:
        full_prompt = payload.get("prompt", "")
        if isinstance(full_prompt, str):
            result["prompt_length"] = len(full_prompt)

    return result


def transform_strip_body(payload: dict[str, object]) -> dict[str, object]:
    """Strip body field, replacing with length and preview."""
    result: dict[str, object] = dict(payload)
    body = payload.get("body", "")
    if not isinstance(body, str):
        body = str(body) if body is not None else ""
    result["body_length"] = len(body)
    result["body_preview"] = body[:200]
    result.pop("body", None)
    return result


# Registry of named transforms -- YAML files reference these by name
TRANSFORM_REGISTRY: dict[str, PayloadTransform] = {
    "passthrough": transform_passthrough,
    "strip_prompt": transform_strip_prompt,
    "strip_body": transform_strip_body,
}


# =============================================================================
# Fan-Out Rule and Registration Models
# =============================================================================


@dataclass(frozen=True)
class FanOutRule:
    """A single fan-out rule specifying a target topic and optional transform.

    Attributes:
        topic: The wire topic name (e.g., "onex.evt.omniclaude.session-started.v1").
        transform: Function to transform the payload before publishing.
        description: Human-readable description of what this rule does.
    """

    topic: str
    transform: PayloadTransform | None = None
    description: str = ""

    def apply_transform(self, payload: dict[str, object]) -> dict[str, object]:
        """Apply the transform to the payload."""
        if self.transform is None:
            return dict(payload)
        return self.transform(payload)


@dataclass(frozen=True)
class EventRegistration:
    """Registration for a single event type with fan-out rules.

    Attributes:
        event_type: Semantic event type identifier (e.g., "prompt.submitted").
        fan_out: List of fan-out rules defining target topics and transforms.
        partition_key_field: Optional field name to use as Kafka partition key.
        required_fields: Field names that must be present in the payload.
    """

    event_type: str
    fan_out: list[FanOutRule] = field(default_factory=list)
    partition_key_field: str | None = None
    required_fields: list[str] = field(default_factory=list)


# =============================================================================
# EventRegistry -- loaded from YAML
# =============================================================================


class EventRegistry:
    """Pluggable event registry loaded from per-platform YAML files.

    Usage:
        registry = EventRegistry.from_yaml(Path("registries/claude_code.yaml"))
        reg = registry.get_registration("prompt.submitted")
    """

    def __init__(
        self, registrations: dict[str, EventRegistration] | None = None
    ) -> None:
        self._registrations: dict[str, EventRegistration] = registrations or {}

    @classmethod
    def from_yaml(cls, path: Path) -> EventRegistry:
        """Load an event registry from a YAML file."""
        with open(path, encoding="utf-8") as f:
            raw = yaml.safe_load(f)

        if not isinstance(raw, dict):
            raise ValueError(f"Registry YAML must be a dict, got {type(raw).__name__}")

        events_raw = raw.get("events", {})
        if not isinstance(events_raw, dict):
            raise ValueError("'events' key must be a dict")

        registrations: dict[str, EventRegistration] = {}

        for event_type, event_def in events_raw.items():
            if not isinstance(event_def, dict):
                logger.warning(f"Skipping non-dict event definition: {event_type}")
                continue

            fan_out_rules: list[FanOutRule] = []
            for rule_def in event_def.get("fan_out", []):
                transform_name = rule_def.get("transform")
                transform_fn: PayloadTransform | None = None
                if transform_name and transform_name != "passthrough":
                    transform_fn = TRANSFORM_REGISTRY.get(transform_name)
                    if transform_fn is None:
                        logger.warning(
                            f"Unknown transform '{transform_name}' for "
                            f"{event_type}, using passthrough"
                        )

                fan_out_rules.append(
                    FanOutRule(
                        topic=rule_def["topic"],
                        transform=transform_fn,
                        description=rule_def.get("description", ""),
                    )
                )

            registrations[event_type] = EventRegistration(
                event_type=event_type,
                fan_out=fan_out_rules,
                partition_key_field=event_def.get("partition_key_field"),
                required_fields=event_def.get("required_fields", []),
            )

        return cls(registrations=registrations)

    @classmethod
    def from_dict(cls, registrations: dict[str, EventRegistration]) -> EventRegistry:
        """Create a registry from a pre-built dict (for testing or programmatic use)."""
        return cls(registrations=registrations)

    def get_registration(self, event_type: str) -> EventRegistration | None:
        """Get the registration for an event type."""
        return self._registrations.get(event_type)

    def list_event_types(self) -> list[str]:
        """List all registered event types."""
        return list(self._registrations.keys())

    def validate_payload(
        self, event_type: str, payload: dict[str, object]
    ) -> list[str]:
        """Validate that a payload has all required fields.

        Returns list of missing field names (empty if valid).
        Raises KeyError if event type is not registered.
        """
        registration = self._registrations.get(event_type)
        if registration is None:
            raise KeyError(f"Unknown event type: {event_type}")
        return [f for f in registration.required_fields if f not in payload]

    def get_partition_key(
        self, event_type: str, payload: dict[str, object]
    ) -> str | None:
        """Extract the partition key from a payload based on registration.

        Raises KeyError if event type is not registered.
        """
        registration = self._registrations.get(event_type)
        if registration is None:
            raise KeyError(f"Unknown event type: {event_type}")
        if registration.partition_key_field is None:
            return None
        value = payload.get(registration.partition_key_field)
        if value is None:
            return None
        return str(value)

    def __len__(self) -> int:
        return len(self._registrations)


__all__: list[str] = [
    "TRANSFORM_REGISTRY",
    "EventRegistration",
    "EventRegistry",
    "FanOutRule",
    "PayloadTransform",
    "transform_passthrough",
    "transform_strip_body",
    "transform_strip_prompt",
]
