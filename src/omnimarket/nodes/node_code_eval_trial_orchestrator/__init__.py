# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Code eval trial orchestrator (OMN-23627): one run of a grid of coding trials.

This package holds the run's node models today (request, nudge, closed event and
the state_io row). The reducer that advances the row is
``omnimarket.nodes.node_code_eval_trial_reducer``.
"""
