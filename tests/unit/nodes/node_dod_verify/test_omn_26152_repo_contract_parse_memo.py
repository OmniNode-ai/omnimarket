# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""OMN-26152: dod_verify parses a repo contract's text once.

A 60 s py-spy sample of the shared dev runtime (2026-11-25, 5305 GIL samples)
held 1973 of them (37.2 percent) in ``parse_repo_contract`` -> ``yaml.safe_load``
under ``run_durable_gate``: the gate re-reads the same ``contracts/<ticket>.yaml``
at the same commit for every merged PR of a ticket and every sweep tick, and the
text of a commit's file never changes. The recorded shape is one contract of
about 295 KB (contracts/OMN-17427.yaml) read again and again.
"""

from __future__ import annotations

from unittest import mock

import yaml

from omnimarket.nodes.node_dod_verify.handlers import dod_evidence_local_source as src
from omnimarket.nodes.node_dod_verify.models.model_durable_evidence_gate import (
    EnumRepoContractReadStatus,
)

READS = 40


def _large_contract_text() -> str:
    doc: dict[str, object] = {
        "schema_version": "1.0.0",
        "ticket_id": "OMN-17427",
        "dod_evidence": [
            {
                "id": f"dod-{i}",
                "description": f"evidence item {i} " * 6,
                "binds_ac": [f"AC{i}"],
                "checks": [
                    {"check_type": "test_passes", "check_value": f"pytest t{i}"}
                ],
            }
            for i in range(800)
        ],
    }
    return yaml.safe_dump(doc)


def test_same_text_is_parsed_once() -> None:
    text = _large_contract_text()
    real = yaml.safe_load
    with mock.patch.object(src.yaml, "safe_load", side_effect=real) as loader:
        reads = [src.parse_repo_contract(text) for _ in range(READS)]
    assert loader.call_count <= 1
    assert all(r.status is EnumRepoContractReadStatus.FOUND for r in reads)
    assert all(r.contract == reads[0].contract for r in reads)


def test_returned_contract_is_not_shared_between_reads() -> None:
    text = "schema_version: '1.0.0'\nticket_id: OMN-1\nitems: [a]\n"
    first = src.parse_repo_contract(text)
    assert first.contract is not None
    first.contract["items"].append("mutated")  # type: ignore[union-attr]
    second = src.parse_repo_contract(text)
    assert second.contract is not None
    assert second.contract["items"] == ["a"]


def test_error_reads_are_unchanged() -> None:
    assert (
        src.parse_repo_contract("- a\n- b\n").error == "contract root is not a mapping"
    )
    bad = src.parse_repo_contract("a: [\n")
    assert bad.status is EnumRepoContractReadStatus.ERROR
    assert bad.error.startswith("YAML parse error")
