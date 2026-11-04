# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden chain of node_transcript_session_scan_effect: contract handler, scan, read, facts (OMN-22259).

The chain is built the way the contract wires it: the handler class named by the contract receives
the overlay and the injected store, scans the roots, hands the bytes read to the facts compute, and
the facts agree with the manifest on the stream kind and the revision.
"""

from __future__ import annotations

import importlib
from pathlib import Path
from typing import Any

import pytest
import yaml

from omnimarket.models.work_classification import EnumStreamKind
from omnimarket.nodes.node_transcript_session_facts_compute.handlers.handler_transcript_session_facts import (
    HandlerTranscriptSessionFacts,
)
from omnimarket.nodes.node_transcript_session_facts_compute.models import (
    ModelSessionFactsRequest,
)
from omnimarket.nodes.node_transcript_session_scan_effect.models import (
    ModelTranscriptReadRequest,
    ModelTranscriptScanRequest,
)
from tests.nodes.node_transcript_session_scan_effect.store_doubles import (
    InMemoryTranscriptSource,
)
from tests.nodes.node_transcript_session_scan_effect.test_scan import (
    SID,
    _overlay,
    _root,
    _session,
    _subagent,
)

pytestmark = pytest.mark.unit

CONTRACT = (
    Path(__file__).resolve().parents[3]
    / "src/omnimarket/nodes/node_transcript_session_scan_effect/contract.yaml"
)


async def test_the_contract_handler_scans_reads_and_the_facts_agree_with_the_manifest() -> (
    None
):
    declared: dict[str, Any] = yaml.safe_load(CONTRACT.read_text(encoding="utf-8"))[
        "handler"
    ]
    handler_cls = getattr(
        importlib.import_module(declared["module"]), declared["class"]
    )
    store = InMemoryTranscriptSource(
        {
            f"{SID}.jsonl": _session(4),
            f"{SID}/subagents/agent-a1.jsonl": _subagent(2),
        }
    )
    handler = handler_cls(overlay=_overlay(_root()), sources={"mem": store})
    manifest = await handler.handle(ModelTranscriptScanRequest())
    assert manifest.manifest_count == 2
    for entry in manifest.entries:
        read = await handler.read_text(
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
        assert facts.source_revision == entry.source_revision
        assert facts.stream_kind is entry.stream_kind
        assert facts.session_id == SID
        if entry.stream_kind is EnumStreamKind.SUBAGENT:
            assert facts.agent_id == "a1"
