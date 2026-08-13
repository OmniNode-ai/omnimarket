#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Seed ``src/omnimarket/configs/seams.v1.yaml`` from the verified
delegation seam graph (OMN-15763, re-derived under OMN-15784).

Source of truth: ``docs/design/2026-08-13-delegation-seam-graph.json``
(OMN-15784 — re-traced against the bare-topic decision of record,
post-``omninode_infra#833``. Supersedes the original
``docs/design/2026-08-08-delegation-seam-graph.json`` (commit 92483f200),
which was generated ~10.5h before ``#833`` reverted the tenant-prefix
premise three of its edges were scored against; that file is preserved with
a SUPERSEDED banner, not deleted — see
docs/design/2026-08-13-delegation-seam-graph.md). That JSON carries
free-text ``producer.shape`` / ``consumer.shape`` descriptions and
both-sides file:line citations, not the field-level structured shape
``ModelSeamProjection`` requires (topic / envelope_model / envelope_version
/ key_fields as separate typed values) — the 2026-08-08 addendum notes that
field-level extraction needs the actual Python model files read, which
``node_seam_graph_compute``'s contract-declared extractor path performs
once producing contracts declare ``seams:`` blocks (proposal step 1, not
yet done for these 15 edges).

Seeding a fabricated ``ModelSeamProjection`` from prose here would invent
structure the source data does not actually carry. Instead this script
pins each edge's VERIFIED SOURCE RECORD (edge id, seam description,
classification, producer/consumer shape prose) with a sha256 computed via
the same canonical-JSON idiom as ``omnimarket.seams.canonical`` — so the
registry is provably reproducible from the JSON and any edit to the source
record changes its pin, without claiming a per-field wire-projection match
that has not actually been extracted yet.

**Two distinct hash namespaces — never conflate them (post-merge correction,
OMN-15763 fix-forward pass).** The field emitted here is named
``source_record_pinned_hash`` and lives entirely in the PROSE-RECORD
namespace: it is ``sha256(canonical_json({edge_id, seam, classification,
producer_shape, consumer_shape}))``. It is NOT comparable to
``omnimarket.seams.canonical.canonical_sha256(ModelSeamProjection)``, which
hashes the typed wire-projection (``seam-projection/v1``) that
``node_seam_match_compute``'s ``check_stale_proof`` expects as
``ModelSeamMatchRequest.pinned_hash``. Feeding this script's pin into that
request field is a type-confusion bug: the two hash spaces share nothing but
the sha256 algorithm, so the comparison reports ``stale=True``
unconditionally regardless of whether the seam actually changed. A sibling
``projection_pinned_hash`` field is also emitted per edge, ``None`` for all
15 rows today because field-level ``ModelSeamProjection`` extraction for
these hand-traced edges has not been done — that is the AC1 gap this
comment documents rather than papers over (node_seam_graph_compute's
contract-declared extractor path populates it once producing contracts
declare real ``seams:`` blocks for these edges, which is still open work).
See ``tests/test_seams_registry_match_integration.py`` for the cross-
boundary proof that ``check_stale_proof`` DOES return "hash pin current"
when it is actually fed a projection-namespace pin.

Regenerable status is honest, not optimistic: the 2026-08-08 methodology
(§0.3) found 1 nominal MATCHED (S10) but ZERO actually regenerable of 15 —
S10 is a contract.yaml-vs-contract.yaml shape comparison, exactly what the
regeneration-boundary rule excludes. Every edge here is seeded
``regenerable: false`` until a real node_seam_match_compute three-leg run
(with live observed projections) proves otherwise.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path
from typing import Any

import yaml

_REPO_ROOT = Path(__file__).resolve().parents[1]
_OUTPUT_PATH = _REPO_ROOT / "src" / "omnimarket" / "configs" / "seams.v1.yaml"


def _resolve_source_json() -> Path | None:
    """Locate the source JSON via $OMNI_HOME — never guess repo-nesting
    depth, since that varies between the canonical clone (omni_home/omnimarket)
    and a worktree checkout (omni_home/omni_worktrees/<ticket>/omnimarket).

    Returns ``None`` (not a raised ``KeyError``) when $OMNI_HOME is unset, so
    ``main()`` reaches its own controlled error-and-exit path rather than a
    raw traceback.
    """

    omni_home_raw = os.environ.get("OMNI_HOME")
    if not omni_home_raw:
        return None
    return (
        Path(omni_home_raw)
        / "docs"
        / "design"
        / "2026-08-13-delegation-seam-graph.json"
    )


def _canonical_json(record: dict[str, Any]) -> str:
    return json.dumps(record, sort_keys=True, separators=(",", ":"))


def _source_record_pin_hash(record: dict[str, Any]) -> str:
    """sha256 of the PROSE-RECORD namespace only — never comparable to
    ``omnimarket.seams.canonical.canonical_sha256(ModelSeamProjection)``.
    See the module docstring's "Two distinct hash namespaces" section."""

    return hashlib.sha256(_canonical_json(record).encode("utf-8")).hexdigest()


def _seed_entry(edge: dict[str, Any]) -> dict[str, Any]:
    producer = edge.get("producer") or {}
    consumer = edge.get("consumer") or {}
    record = {
        "edge_id": edge["id"],
        "seam": edge["seam"],
        "classification": edge["classification"],
        "producer_shape": producer.get("shape"),
        "consumer_shape": consumer.get("shape"),
    }
    return {
        "edge_id": edge["id"],
        "seam": edge["seam"],
        "leg": edge.get("leg"),
        "classification": edge["classification"],
        "severity": edge.get("severity"),
        "producer_shape": producer.get("shape"),
        "consumer_shape": consumer.get("shape"),
        "regenerable": False,
        "source_record_pinned_hash": _source_record_pin_hash(record),
        # ModelSeamProjection-namespace pin (seam-projection/v1,
        # omnimarket.seams.canonical.canonical_sha256): None until real
        # field-level extraction exists for this hand-traced edge — see the
        # module docstring. Never populate this with a source-record hash.
        "projection_pinned_hash": None,
        "related_tickets": edge.get("related_tickets", []),
    }


def build_registry(source_json_path: Path) -> dict[str, Any]:
    data = json.loads(source_json_path.read_text(encoding="utf-8"))
    edges = sorted(data["edges"], key=lambda e: e["id"])
    entries = [_seed_entry(edge) for edge in edges]
    regenerable_count = sum(1 for entry in entries if entry["regenerable"])
    matched_count = sum(1 for entry in entries if entry["classification"] == "MATCHED")
    # Derived from the actually-resolved source path (never hand-typed) so the
    # registry cannot silently cite a stale filename the way the 2026-08-08
    # snapshot did after #833 revalidated its own premise (OMN-15784).
    source_path_str = f"docs/design/{source_json_path.name}"
    return {
        "schema_version": "seams-registry/v1",
        "generated_from": {
            "source_path": source_path_str,
            "source_schema_version": data.get("schema_version"),
            "generated_at": data.get("generated_at"),
        },
        "summary": {
            "edges_total": len(entries),
            "matched_count": matched_count,
            "regenerable_count": regenerable_count,
            "by_classification": data.get("summary_counts", {}),
        },
        "edges": entries,
    }


def main() -> int:
    source_json = _resolve_source_json()
    if source_json is None:
        print("OMNI_HOME is not set — cannot locate the source JSON", file=sys.stderr)
        return 1
    if not source_json.exists():
        print(f"source JSON not found: {source_json}", file=sys.stderr)
        return 1
    registry = build_registry(source_json)
    _OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    header = (
        "# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.\n"
        "# SPDX-License-Identifier: MIT\n"
        "# GENERATED by scripts/seed_seams_registry.py (OMN-15763) — do not hand-edit.\n"
        f"# Source of truth: docs/design/{source_json.name}\n"
        "# Regenerate: uv run python scripts/seed_seams_registry.py\n"
    )
    with _OUTPUT_PATH.open("w", encoding="utf-8") as handle:
        handle.write(header)
        yaml.safe_dump(registry, handle, sort_keys=False, default_flow_style=False)
    print(f"wrote {_OUTPUT_PATH} ({len(registry['edges'])} edges)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
