# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""node_typed_decision_effect: one typed question to a contract-pinned decision backend.

Unwired: no event_bus block and no ``onex.nodes`` entry point. It is invoked
in-process (``python -m omnimarket.nodes.node_typed_decision_effect``) until a
decision contract composes it.
"""
