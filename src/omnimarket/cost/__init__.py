# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Canonical omnimarket cost surface (OMN-13621).

Routed from the SEA hackathon repo (``onex-self-extending-agent``) as part of the
SEA -> canonical migration (epic OMN-13604). Owns:

- ``usage_normalizer`` — provider-aware token-usage normalization
  (OpenAI-compatible / Gemini-native / char-length estimate fallback).
- ``cost_pricing`` — contract-sourced per-model pricing
  (``cost_pricing.yaml``), with typed load / validate / lookup / calculate.

Generation cost is computed from this surface (contract-sourced, not hardcoded)
and lands in the canonical cost projection (``generation_events.cost_inference_usd``)
via ``node_projection_delegation`` — the same projection-write path proven for
delegation cost telemetry in OMN-13408.
"""
