# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Event mapper utility for intent event consumer.

Maps incoming intent-classified events to storage requests.
"""

from omnibase_core.enums.intelligence import EnumIntentCategory
from omnibase_core.models.intelligence import (
    ModelIntentClassificationOutput,
)
from omnimemory.models.events import ModelIntentClassifiedEvent

from omnimarket.nodes.node_intent_storage_effect.models import ModelIntentStorageRequest


def map_event_to_storage_request(
    event: ModelIntentClassifiedEvent,
) -> ModelIntentStorageRequest:
    """Map incoming classified event to storage request.

    Uses ``event.intent_class.value`` (the canonical field) as the intent
    category string for storage. ``EnumIntentClass`` values (e.g. ``"bugfix"``,
    ``"feature"``) may not have a 1:1 match with ``EnumIntentCategory`` — unknown
    values fall back to ``EnumIntentCategory.UNKNOWN`` for forward compatibility.

    Args:
        event: The incoming intent-classified event from omniintelligence.

    Returns:
        A storage request ready for HandlerIntentStorageAdapter.
    """
    try:
        intent_category: EnumIntentCategory = EnumIntentCategory(
            event.intent_class.value  # Canonical: use .value at boundary (OMN-3248)
        )
    except ValueError:
        intent_category = EnumIntentCategory.UNKNOWN

    return ModelIntentStorageRequest(
        operation="store",
        session_id=event.session_id,
        intent_data=ModelIntentClassificationOutput(
            success=True,
            intent_category=intent_category,
            confidence=event.confidence,
            keywords=list(event.keywords),
        ),
        correlation_id=event.correlation_id,
    )
