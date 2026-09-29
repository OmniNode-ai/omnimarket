# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Handlers for the lab container memory projection (OMN-19961).

A rule-7a pair: ``handler_container_memory_fold`` (pure) and
``handler_container_memory_writer`` (the effect-class writer the runtime
calls). Neither is imported here, so importing the pure fold never pulls the
writer's Kafka and asyncpg stack in with it.
"""
