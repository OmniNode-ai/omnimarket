# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Compatibility import for the delegation effect's agent-session entrypoint.

Pool hosts execute the effect's standalone handler directly; the morning
orchestrator owns no model-call transport.
"""

import sys

from omnimarket.nodes.node_llm_delegation_call_effect.handlers.handler_agent_session import (
    main,
)

__all__ = ["main"]


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
