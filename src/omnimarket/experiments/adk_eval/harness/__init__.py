# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Measurement harness for the ADK evaluation spike (P8).

Aggregates per-track self-reported latency/token metrics, scorer F1
results, and rough wall-clock dev-time estimates into a single
`measurements.json` for the P10 Decision Gate write-up.

No re-runs: reads pre-emitted evidence files; the harness only joins +
computes ratios. All cost numbers are marginal per-run inference cost;
dev-time is rough single-developer wall-clock, not rigorous TCO.
"""

from omnimarket.experiments.adk_eval.harness.aggregator import aggregate

__all__ = ["aggregate"]
