# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-14785 (parent OMN-14783): F-17 label read-side parity for the OCC producer.

The bespoke ``OccCompanionEmitter`` suppresses a companion when the product PR
carries a do-not-merge/WIP LABEL (not just a title/body marker). For the
canonical ``node_occ_companion_*`` graph to reach parity before the emitter is
retired, the read-EFFECT must carry the PR label names onto the compute seam.

These exercise ONLY the pure ``extract_pr_label_names`` transform (mirroring how
``test_symbol_derivation`` unit-tests the pure functions and leaves the network
half ``_gather_sync`` to the live canary), so the F-17 label seam is provable
without a network mock.
"""

from __future__ import annotations

from omnimarket.nodes.node_occ_state_effect.handlers.handler_occ_state_effect import (
    extract_pr_label_names,
)


class TestExtractPrLabelNames:
    def test_extracts_label_names_in_order(self) -> None:
        pr = {"labels": [{"name": "bug"}, {"name": "do-not-merge"}]}
        assert extract_pr_label_names(pr) == ("bug", "do-not-merge")

    def test_missing_labels_key_yields_empty(self) -> None:
        assert extract_pr_label_names({"number": 1}) == ()

    def test_non_list_labels_yields_empty(self) -> None:
        assert extract_pr_label_names({"labels": "do-not-merge"}) == ()

    def test_labels_without_name_are_dropped(self) -> None:
        pr = {"labels": [{"color": "red"}, {"name": ""}, {"name": "WIP"}]}
        assert extract_pr_label_names(pr) == ("WIP",)

    def test_non_dict_label_entries_are_dropped(self) -> None:
        pr = {"labels": ["do-not-merge", {"name": "area:ci"}]}
        assert extract_pr_label_names(pr) == ("area:ci",)
