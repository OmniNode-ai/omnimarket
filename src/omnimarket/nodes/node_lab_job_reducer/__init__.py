# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Lab job reducer: the pure transition function of the lab job supervisor.

``HandlerLabJobReducer.handle(ModelLabJobReduceInput) -> ModelLabJobReduceOutput``
(definition-B). The job orchestrator calls it in process; it has no bus
surface of its own.
"""
