# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The scan and facts scenario behaves the same under the in-memory baseline and a Redpanda profile (OMN-22259, T1, AC9).

The scan depends on one protocol, the transcript store (TRN). Each profile's binding yields the
store the scenario runs on; the manifest and the facts it yields are projected into the parity run's
rows and compared by the parity run's own comparison. A binding that drops a file, moves a revision or
reorders the manifest makes the comparison fail.
"""

from __future__ import annotations

import json
from collections.abc import Callable

import pytest

from omnimarket.nodes.node_binding_parity_orchestrator.handlers.handler_compare_runs import (
    compare_runs,
)
from omnimarket.nodes.node_binding_parity_orchestrator.models.model_parity import (
    ModelCompareResult,
    ModelScenarioRow,
    ModelScenarioRun,
)
from omnimarket.nodes.node_binding_parity_orchestrator.overlay import (
    ModelFactoryBindingsOverlay,
)
from omnimarket.nodes.node_transcript_session_facts_compute.handlers.handler_transcript_session_facts import (
    HandlerTranscriptSessionFacts,
)
from omnimarket.nodes.node_transcript_session_facts_compute.models import (
    ModelSessionFactsRequest,
)
from omnimarket.nodes.node_transcript_session_scan_effect.handlers.handler_transcript_session_scan import (
    HandlerTranscriptSessionScan,
)
from omnimarket.nodes.node_transcript_session_scan_effect.models import (
    ModelTranscriptReadRequest,
    ModelTranscriptScanRequest,
)
from omnimarket.nodes.node_transcript_session_scan_effect.overlay import (
    load_scan_overlay,
)
from tests.nodes.node_transcript_session_scan_effect.store_doubles import (
    InMemoryTranscriptSource,
)
from tests.nodes.node_transcript_session_scan_effect.test_scan import (
    _line,
    _session,
    _subagent,
)

pytestmark = pytest.mark.unit

CODES = ("TRN",)


def _objects() -> dict[str, bytes]:
    return {
        "s1.jsonl": _session(4)
        + _line(type="pr-link", prUrl="https://example.test/pr/1").encode(),
        "s2.jsonl": _session(2),
        "s1/subagents/agent-a1.jsonl": _subagent(3),
    }


def reference() -> InMemoryTranscriptSource:
    return InMemoryTranscriptSource(_objects())


def drops_a_file() -> InMemoryTranscriptSource:
    objects = _objects()
    del objects["s2.jsonl"]
    return InMemoryTranscriptSource(objects)


def moves_a_revision() -> InMemoryTranscriptSource:
    objects = _objects()
    objects["s2.jsonl"] += _line(type="progress").encode()
    return InMemoryTranscriptSource(objects)


BINDINGS: dict[str, Callable[[], InMemoryTranscriptSource]] = {
    "reference": reference,
    "drops_a_file": drops_a_file,
    "moves_a_revision": moves_a_revision,
}


def overlay(redpanda_binding: str) -> ModelFactoryBindingsOverlay:
    return ModelFactoryBindingsOverlay.model_validate(
        {
            "profiles": [{"id": "in-memory", "baseline": True}, {"id": "redpanda"}],
            "bindings": {
                "in-memory": dict.fromkeys(CODES, "reference"),
                "redpanda": dict.fromkeys(CODES, redpanda_binding),
            },
            "proof_bindings": ["reference"],
        }
    )


async def run_scenario(profile_id: str, binding_id: str) -> ModelScenarioRun:
    store = BINDINGS[binding_id]()
    overlay_model = load_scan_overlay(
        {},
        explicit={
            "transcript_roots": [
                {"name": "mem", "handler": "memory", "location": "fixture"}
            ],
            "manifest_hash_algorithm": "sha256",
            "read_max_bytes": 1_000_000,
        },
    )
    scan = HandlerTranscriptSessionScan(overlay=overlay_model, sources={"mem": store})
    manifest = await scan.handle(ModelTranscriptScanRequest())
    rows = []
    for entry in manifest.entries:
        read = await scan.read_text(
            ModelTranscriptReadRequest(
                root_name=entry.root_name,
                relative_path=entry.relative_path,
                scanned_source_revision=entry.source_revision,
            )
        )
        facts = HandlerTranscriptSessionFacts().handle(
            ModelSessionFactsRequest(
                transcript_text=read.transcript_text,
                required_source_revision=read.required_source_revision,
            )
        )
        rows.append(
            ModelScenarioRow(
                key=entry.stream_id,
                content=json.dumps(
                    {
                        "path": entry.relative_path,
                        "kind": entry.stream_kind.value,
                        "revision": entry.source_revision.model_dump(),
                        "facts": facts.model_dump(mode="json"),
                    },
                    sort_keys=True,
                ),
            )
        )
    return ModelScenarioRun(
        profile_id=profile_id,
        binding=profile_id,
        proof_eligible=True,
        verdicts=("PASS",),
        lines=tuple(f"scanned {e.relative_path}" for e in manifest.entries),
        rows=tuple(rows),
        counts={
            "manifest": manifest.manifest_count,
            "top_level": manifest.top_level_count,
            "subagent": manifest.subagent_count,
            "facts": len(rows),
        },
    )


async def parity(redpanda_binding: str) -> ModelCompareResult:
    chosen = overlay(redpanda_binding)
    runs = tuple(
        [
            await run_scenario(p.id, chosen.binding_id(p.id, "TRN"))
            for p in chosen.profiles
        ]
    )
    return compare_runs(runs)


async def test_the_scan_and_facts_project_the_same_under_both_profiles() -> None:
    result = await parity("reference")
    assert result.passed, result.first_difference
    baseline = await run_scenario("in-memory", "reference")
    assert baseline.counts == {"manifest": 3, "top_level": 2, "subagent": 1, "facts": 3}


@pytest.mark.parametrize("binding", ["drops_a_file", "moves_a_revision"])
async def test_a_binding_that_drops_a_file_or_moves_a_revision_fails_the_comparison(
    binding: str,
) -> None:
    result = await parity(binding)
    assert not result.passed
    assert result.first_difference
