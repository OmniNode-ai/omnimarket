# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The OMN-23136 gate tests, collected from this node's own path.

The tests read the baseline file and build reading rows, which the node's purity checks forbid
inside a node package, so they live under tests/unit/nodes/node_network_graph_compute and are
re-exported here; the ticket's falsifier commands run pytest over this directory.
"""

from __future__ import annotations

import pytest

from tests.unit.nodes.node_network_graph_compute.test_dependency_gate import (
    test_break_a_gate_that_never_reads_red_turns_the_error_chain_assertion_red,
    test_dependency_baseline_frozen_break_a_changed_number_without_a_new_hash_reads_red,
    test_dependency_baseline_frozen_holds_the_corrected_counts_and_its_hash,
    test_dependency_baseline_frozen_is_read_by_the_gate_and_the_fixture_passes,
    test_error_chain_a_bad_gate_request_is_the_typed_failed_event_never_dropped,
    test_error_chain_a_planted_back_edge_is_a_computed_event_that_reads_red,
    test_golden_chain_the_gate_command_gives_one_computed_event_that_reads_pass,
    test_planted_back_edge_a_new_subsystem_in_the_component_goes_red,
    test_planted_back_edge_on_a_frozen_relation_names_that_relation,
    test_planted_back_edge_raises_the_count_above_the_frozen_value_and_goes_red,
    test_reading_row_carries_counts_above_the_frozen_value_when_a_back_edge_is_planted,
    test_reading_row_carries_counts_current_and_frozen_in_the_written_row,
    test_reading_row_carries_counts_error_chain_a_bad_gate_payload_is_a_failed_event,
    test_reading_row_carries_counts_error_chain_a_refused_append_is_a_failed_event,
    test_reading_row_carries_counts_error_chain_an_unread_gate_is_unread_never_zero,
    test_reading_row_carries_counts_golden_chain_one_written_event_and_one_row,
    test_the_baseline_file_is_plain_yaml_with_the_hash_cell,
)

pytestmark = pytest.mark.unit

__all__ = [
    "test_break_a_gate_that_never_reads_red_turns_the_error_chain_assertion_red",
    "test_dependency_baseline_frozen_break_a_changed_number_without_a_new_hash_reads_red",
    "test_dependency_baseline_frozen_holds_the_corrected_counts_and_its_hash",
    "test_dependency_baseline_frozen_is_read_by_the_gate_and_the_fixture_passes",
    "test_error_chain_a_bad_gate_request_is_the_typed_failed_event_never_dropped",
    "test_error_chain_a_planted_back_edge_is_a_computed_event_that_reads_red",
    "test_golden_chain_the_gate_command_gives_one_computed_event_that_reads_pass",
    "test_planted_back_edge_a_new_subsystem_in_the_component_goes_red",
    "test_planted_back_edge_on_a_frozen_relation_names_that_relation",
    "test_planted_back_edge_raises_the_count_above_the_frozen_value_and_goes_red",
    "test_reading_row_carries_counts_above_the_frozen_value_when_a_back_edge_is_planted",
    "test_reading_row_carries_counts_current_and_frozen_in_the_written_row",
    "test_reading_row_carries_counts_error_chain_a_bad_gate_payload_is_a_failed_event",
    "test_reading_row_carries_counts_error_chain_a_refused_append_is_a_failed_event",
    "test_reading_row_carries_counts_error_chain_an_unread_gate_is_unread_never_zero",
    "test_reading_row_carries_counts_golden_chain_one_written_event_and_one_row",
    "test_the_baseline_file_is_plain_yaml_with_the_hash_cell",
]
