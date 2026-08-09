# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Regression coverage for src/omnimarket/configs/seams.v1.yaml (OMN-15763).

The registry is GENERATED (scripts/seed_seams_registry.py), not
hand-maintained, from the verified delegation seam graph
(docs/design/2026-08-08-delegation-seam-graph.json, commit 92483f200).
These tests are themselves a stale-proof check at the registry-file
granularity: if the checked-in YAML ever drifts from what the generator
would produce right now (source edited without re-running the script, or
the script's hashing changed without a re-pin), the mismatching edge is
named explicitly.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
import yaml

from scripts.seed_seams_registry import _pin_hash, build_registry

_REGISTRY_PATH = Path("src/omnimarket/configs/seams.v1.yaml")


def _load_registry() -> dict[str, object]:
    raw = yaml.safe_load(_REGISTRY_PATH.read_text(encoding="utf-8"))
    assert isinstance(raw, dict)
    return raw


def _source_json_path() -> Path | None:
    """``None`` when $OMNI_HOME is unset or the source doc is unreachable —
    e.g. in the product-repo CI runner, which checks out only this repo, not
    the omni_home meta-repo the design doc lives in. The stale-proof check
    below is best-effort/local-dev-only for that reason; the checked-in
    registry is still durable evidence on its own, receipted in the PR body
    that generated it."""

    omni_home_raw = os.environ.get("OMNI_HOME")
    if not omni_home_raw:
        return None
    candidate = (
        Path(omni_home_raw)
        / "docs"
        / "design"
        / "2026-08-08-delegation-seam-graph.json"
    )
    return candidate if candidate.exists() else None


@pytest.mark.unit
class TestSeamsRegistryShape:
    def test_registry_file_exists(self) -> None:
        assert _REGISTRY_PATH.exists()

    def test_schema_version_pinned(self) -> None:
        registry = _load_registry()
        assert registry["schema_version"] == "seams-registry/v1"

    def test_carries_all_fifteen_delegation_edges(self) -> None:
        registry = _load_registry()
        edges = registry["edges"]
        assert isinstance(edges, list)
        assert len(edges) == 15
        edge_ids = {edge["edge_id"] for edge in edges}
        assert edge_ids == {f"S{n}" for n in range(1, 16)}

    def test_every_edge_has_a_64_char_hex_pin(self) -> None:
        registry = _load_registry()
        for edge in registry["edges"]:
            pinned = edge["pinned_hash"]
            assert len(pinned) == 64
            int(pinned, 16)  # raises ValueError if not hex

    def test_regenerable_count_reported_separately_from_matched_count(self) -> None:
        registry = _load_registry()
        summary = registry["summary"]
        # §0.3 regeneration-boundary rule, honest count 2026-08-08: 1 nominal
        # MATCHED (S10, a shape-only contract.yaml-vs-contract.yaml comparison)
        # but ZERO actually regenerable of 15 — a shape match never counts as
        # regenerable on its own.
        assert summary["matched_count"] == 1
        assert summary["regenerable_count"] == 0
        assert summary["matched_count"] != summary["regenerable_count"] or (
            summary["matched_count"] == 0
        )


@pytest.mark.unit
class TestSeamsRegistryStaleProof:
    """The registry-level analogue of node_seam_match_compute's stale-proof
    detector: every checked-in pin must match a fresh hash computed from the
    live source JSON via the same canonical-JSON idiom."""

    def test_every_pin_matches_a_fresh_regeneration(self) -> None:
        source_json_path = _source_json_path()
        if source_json_path is None:
            pytest.skip(
                "OMNI_HOME unset or docs/design/2026-08-08-delegation-seam-graph.json "
                "not reachable in this environment (expected in product-repo CI, "
                "which does not check out the omni_home meta-repo)"
            )

        fresh = build_registry(source_json_path)
        checked_in = _load_registry()

        fresh_by_id = {edge["edge_id"]: edge for edge in fresh["edges"]}
        checked_in_by_id = {edge["edge_id"]: edge for edge in checked_in["edges"]}

        # The edge-ID SET itself must match — a new/removed source edge is
        # staleness too, not just a changed pin on an edge both sides agree
        # exists.
        assert set(checked_in_by_id) == set(fresh_by_id), (
            "seam changed, proof stale: registry edge-ID set does not match a "
            "fresh regeneration from the source JSON (edge added/removed) — "
            "re-run scripts/seed_seams_registry.py. "
            f"checked-in-only: {sorted(set(checked_in_by_id) - set(fresh_by_id))}; "
            f"fresh-only: {sorted(set(fresh_by_id) - set(checked_in_by_id))}"
        )

        for edge_id, checked_in_edge in checked_in_by_id.items():
            fresh_edge = fresh_by_id[edge_id]
            # Compare the COMPLETE generated record, not just pinned_hash —
            # catches a changed unpinned generated field (e.g. severity,
            # related_tickets) that a hash-only comparison would miss.
            assert checked_in_edge == fresh_edge, (
                f"seam changed, proof stale: {edge_id} record does not match "
                "a fresh regeneration from the source JSON — re-run "
                "scripts/seed_seams_registry.py. "
                f"checked-in: {checked_in_edge}; fresh: {fresh_edge}"
            )

    def test_pin_hash_is_deterministic_across_repeat_calls(self) -> None:
        record = {
            "edge_id": "S1",
            "seam": "x",
            "producer_shape": "a",
            "consumer_shape": "b",
        }
        assert _pin_hash(record) == _pin_hash(record)

    def test_pin_hash_changes_when_record_field_changes(self) -> None:
        a = {"edge_id": "S1", "seam": "x", "producer_shape": "a", "consumer_shape": "b"}
        b = {
            "edge_id": "S1",
            "seam": "x",
            "producer_shape": "a-changed",
            "consumer_shape": "b",
        }
        assert _pin_hash(a) != _pin_hash(b)
