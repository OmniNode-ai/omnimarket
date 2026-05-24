# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

# Copyright (c) 2025 OmniNode Team
"""Handler class for Intelligence Reducer node.

Wraps the free handler functions into a class so the runtime can
discover and wire this handler via handler_routing in contract.yaml.

Routing:
    - ModelReducerInputPatternLifecycle  -> handle_pattern_lifecycle_process
    - All other FSM types fall through to the NodeReducer base class

The runtime instantiates HandlerIntelligenceReducer and calls handle().
Topic-based dispatch is managed by the node.py process() method which
inspects the FSM type discriminator before delegating here.

Ticket: OMN-11759
"""

from __future__ import annotations

import logging

from omnibase_core.models.reducer.model_reducer_output import ModelReducerOutput

from omnimarket.nodes.node_intelligence_reducer.handlers.handler_process import (
    handle_pattern_lifecycle_process,
)
from omnimarket.nodes.node_intelligence_reducer.models.model_intelligence_state import (
    ModelIntelligenceState,
)
from omnimarket.nodes.node_intelligence_reducer.models.model_reducer_input import (
    ModelReducerInput,
    ModelReducerInputPatternLifecycle,
)

logger = logging.getLogger(__name__)


class HandlerIntelligenceReducer:
    """Handler class for Intelligence Reducer node.

    Dispatches to the appropriate handler function based on the
    FSM type discriminator in the input model.

    Handlers:
        - PATTERN_LIFECYCLE: handle_pattern_lifecycle_process
        - INGESTION, PATTERN_LEARNING, QUALITY_ASSESSMENT: not handled here
          (delegated to NodeReducer base class via node.py)
    """

    def handle(
        self,
        input_data: ModelReducerInput | ModelReducerInputPatternLifecycle,
    ) -> ModelReducerOutput[ModelIntelligenceState]:
        """Dispatch reducer input to the appropriate handler function.

        Args:
            input_data: Discriminated reducer input. PATTERN_LIFECYCLE type
                routes to handle_pattern_lifecycle_process. All other FSM
                types are not expected here — they are handled by the base
                class FSM execution path in node.py.

        Returns:
            ModelReducerOutput with typed state and intents.

        Raises:
            TypeError: If an unexpected FSM type is passed that is not
                PATTERN_LIFECYCLE (should not happen in normal operation).
        """
        if isinstance(input_data, ModelReducerInputPatternLifecycle):
            logger.debug(
                "HandlerIntelligenceReducer routing to handle_pattern_lifecycle_process",
                extra={"fsm_type": "PATTERN_LIFECYCLE"},
            )
            return handle_pattern_lifecycle_process(input_data)

        # Other FSM types (INGESTION, PATTERN_LEARNING, QUALITY_ASSESSMENT)
        # are delegated to the NodeReducer base class in node.py.
        # This path should not be reached at runtime for those types.
        raise TypeError(
            f"HandlerIntelligenceReducer.handle() received unexpected FSM type "
            f"'{getattr(input_data, 'fsm_type', type(input_data).__name__)}'. "
            "Non-PATTERN_LIFECYCLE inputs must be handled by NodeReducer base class."
        )


__all__ = ["HandlerIntelligenceReducer"]
