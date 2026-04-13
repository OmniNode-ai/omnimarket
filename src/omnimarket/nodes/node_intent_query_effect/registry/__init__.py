# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

# Copyright (c) 2025 OmniNode Team
"""Registry for intent_query_effect node.

Provides factory methods and metadata for the intent_query_effect node,
following ONEX registry patterns for dependency injection and service discovery.

Topic Discovery:
    Topics are declared in this node's ``contract.yaml`` (``event_bus`` section)
    and discovered at runtime via ``omnimemory.runtime.contract_topics``.
    See ``collect_subscribe_topics_from_contracts()`` and
    ``collect_publish_topics_for_dispatch()`` for contract-driven topic discovery.

Example::

    from omnimarket.nodes.node_intent_query_effect.registry import (
        RegistryIntentQueryEffect,
    )

    # Create handler via registry
    handler = await RegistryIntentQueryEffect.create_and_initialize(adapter)

    # Query node metadata
    node_type = RegistryIntentQueryEffect.get_node_type()  # "EFFECT"

.. versionadded:: 0.1.0
    Initial implementation for OMN-1504.

.. versionchanged:: 0.3.0
    Removed get_topic_suffixes() -- topics are now contract-driven (OMN-2213).
"""

from omnimarket.nodes.node_intent_query_effect.registry.registry_intent_query_effect import (
    RegistryIntentQueryEffect,
)

__all__ = ["RegistryIntentQueryEffect"]
