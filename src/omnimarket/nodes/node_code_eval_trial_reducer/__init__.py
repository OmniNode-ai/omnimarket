# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Code eval trial reducer: the pure transition function of one run's row (OMN-23627).

``HandlerCodeEvalTrialReducer.handle(ModelCodeEvalReduceInput) ->
ModelCodeEvalReduceOutput``, definition-B, called in process by the orchestrator;
it has no bus surface. Modelled in formal/code_eval_trial.
"""
