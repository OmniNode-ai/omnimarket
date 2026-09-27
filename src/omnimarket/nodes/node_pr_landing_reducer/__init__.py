# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""PR landing reducer: the pure transition function of the landing FSM.

``HandlerPrLandingReducer.handle(ModelPrLandingReduceInput) ->
ModelPrLandingReduceOutput`` (definition-B). The orchestrator calls it in
process; it has no bus surface of its own.
"""
