# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The OMN-23138 lane intake create tests, collected from this node's own path.

They build in-memory buses and ticket stand-ins, which the node's purity checks forbid inside a
node package, so they live under tests/unit/nodes/node_create_ticket and are re-exported here;
the ticket's falsifier commands run pytest over this directory.
"""

from __future__ import annotations

import pytest

from tests.unit.nodes.node_create_ticket.test_lane_intake_command_result import (
    test_chain_error_mutation_reads_failure_as_success_turns_the_error_chain_red,
    test_chain_error_mutation_timeout_as_created_turns_the_error_chain_red,
    test_chain_golden_mutation_drops_the_ticket_turns_the_golden_chain_red,
    test_lane_intake_command_result_a_retry_replays_and_creates_once,
    test_lane_intake_command_result_a_typed_refusal_reaches_the_caller_as_a_failure,
    test_lane_intake_command_result_an_unreadable_terminal_is_a_typed_failure,
    test_lane_intake_command_result_creates_the_ticket_by_command,
    test_lane_intake_command_result_error_chain_event_reads_clean,
    test_lane_intake_command_result_golden_chain_event_reads_clean,
    test_lane_intake_command_result_the_callers_key_wins_over_the_derived_one,
)

pytestmark = pytest.mark.unit

__all__ = [
    "test_chain_error_mutation_reads_failure_as_success_turns_the_error_chain_red",
    "test_chain_error_mutation_timeout_as_created_turns_the_error_chain_red",
    "test_chain_golden_mutation_drops_the_ticket_turns_the_golden_chain_red",
    "test_lane_intake_command_result_a_retry_replays_and_creates_once",
    "test_lane_intake_command_result_a_typed_refusal_reaches_the_caller_as_a_failure",
    "test_lane_intake_command_result_an_unreadable_terminal_is_a_typed_failure",
    "test_lane_intake_command_result_creates_the_ticket_by_command",
    "test_lane_intake_command_result_error_chain_event_reads_clean",
    "test_lane_intake_command_result_golden_chain_event_reads_clean",
    "test_lane_intake_command_result_the_callers_key_wins_over_the_derived_one",
]
