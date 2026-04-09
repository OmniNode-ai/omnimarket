# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Ticket Pipeline Node

Manages the ticket processing pipeline with phase-based execution
and event-driven lifecycle management.

Components:
- PipelineStartCommand: Initiates ticket processing
- PipelinePhaseEvent: Tracks processing phases
- PipelineCompletedEvent: Signals completion
- PipelineState: Maintains pipeline status
"""
