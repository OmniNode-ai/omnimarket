# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Regression-test ORCHESTRATOR node package (OMN-13616).

Canonical home for the SEA regression suite (``regression/runner.py``,
``tasks.py``, ``results.py``). Replays a recorded event corpus deterministically
and emits the canonical :class:`ModelExperimentResult` (OMN-13613) on the
terminal topic. Part of the SEA -> canonical migration epic OMN-13604.
"""
