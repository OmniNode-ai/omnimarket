# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden/error chains: pinned operator snapshots through the local event bus."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
import yaml
from omnibase_core.enums.enum_workflow_result import EnumWorkflowResult

from omnimarket.models.replay_bundle import ManifestRefusalError
from omnimarket.models.transcript_manifest import MissingRequiredVariableError
from omnimarket.nodes.node_transcript_manifest_effect import (
    NodeTranscriptManifestEffect,
)
from omnimarket.nodes.node_transcript_manifest_effect.handlers.handler_transcript_manifest_source import (
    HandlerTranscriptManifestSource,
)
from omnimarket.nodes.node_transcript_manifest_effect.models.model_transcript_manifest_source import (
    ModelTranscriptManifestSourceRequest,
)
from tests.runtime_local_compat import RuntimeLocal

from .test_transcript_jsonl_manifest import LEDGER, pins, three_boundary_transcript

pytestmark = pytest.mark.unit


def frozen_corpus(root: Path) -> dict[str, str]:
    transcripts = root / "transcripts"
    transcripts.mkdir(parents=True)
    body = three_boundary_transcript()
    (transcripts / "one.jsonl").write_bytes(body)
    (root / "ledger.md").write_bytes(LEDGER)
    index = {
        "source_mode": "frozen",
        "transcripts": [
            {"relative_path": "one.jsonl", "sha256": hashlib.sha256(body).hexdigest()}
        ],
        "ledger_files": [
            {"relative_path": "ledger.md", "sha256": hashlib.sha256(LEDGER).hexdigest()}
        ],
        "summary_pins": [pin.model_dump(mode="json") for pin in pins()],
        "obligation_counts": [
            {"boundary_uuid": f"b{i}", "must_survive_items": 5} for i in range(1, 4)
        ],
    }
    (root / "cm1_sources.json").write_text(json.dumps(index))
    return {
        "ONEX_TRANSCRIPT_ROOT": str(transcripts),
        "ONEX_CONTEXT_REPLAY_ROOT": str(root),
    }


@pytest.mark.parametrize(
    "variable", ["ONEX_TRANSCRIPT_ROOT", "ONEX_CONTEXT_REPLAY_ROOT"]
)
@pytest.mark.parametrize("value", [None, "", "   "])
def test_unset_replay_root_source_fails_before_reading(
    variable: str,
    value: str | None,
    tmp_path: Path,
) -> None:
    env = {
        "ONEX_TRANSCRIPT_ROOT": str(tmp_path / "missing"),
        "ONEX_CONTEXT_REPLAY_ROOT": str(tmp_path / "missing"),
    }
    if value is None:
        del env[variable]
    else:
        env[variable] = value
    with pytest.raises(MissingRequiredVariableError) as caught:
        HandlerTranscriptManifestSource(environ=env).handle(
            ModelTranscriptManifestSourceRequest()
        )
    assert caught.value.variable == variable
    assert variable in str(caught.value)


def test_source_builds_a_reproducible_bundle_without_returning_transcript_text(
    tmp_path: Path,
) -> None:
    env = frozen_corpus(tmp_path)
    handler = HandlerTranscriptManifestSource(environ=env)
    first = handler.handle(ModelTranscriptManifestSourceRequest())
    second = handler.handle(ModelTranscriptManifestSourceRequest())
    assert first == second
    assert first.cases == 2
    assert first.boundaries == 3
    assert first.exclusion_counts == (("RECOMPACTION", 1),)
    manifest = tmp_path / first.manifest_path
    assert hashlib.sha256(manifest.read_bytes()).hexdigest() == first.manifest_sha256
    document = yaml.safe_load(manifest.read_text())
    assert len(document["cases"]) == 2
    for case in document["cases"]:
        prefix = manifest.parent / case["prefix_bundle_path"]
        assert hashlib.sha256(prefix.read_bytes()).hexdigest() == case["prefix_sha256"]
    assert "Do not merge" not in first.model_dump_json()


def test_source_refuses_a_tampered_snapshot(tmp_path: Path) -> None:
    env = frozen_corpus(tmp_path)
    (tmp_path / "transcripts/one.jsonl").write_bytes(b"tampered")
    with pytest.raises(ManifestRefusalError, match="sha256 mismatch"):
        HandlerTranscriptManifestSource(environ=env).handle(
            ModelTranscriptManifestSourceRequest()
        )
    assert not (tmp_path / "manifests").exists()


def test_source_refuses_live_transcripts_outside_replay_root(tmp_path: Path) -> None:
    env = frozen_corpus(tmp_path / "replay")
    env["ONEX_TRANSCRIPT_ROOT"] = str(tmp_path / "live")
    with pytest.raises(ManifestRefusalError, match="inside ONEX_CONTEXT_REPLAY_ROOT"):
        HandlerTranscriptManifestSource(environ=env).handle(
            ModelTranscriptManifestSourceRequest()
        )


def test_source_refuses_an_unindexed_transcript(tmp_path: Path) -> None:
    env = frozen_corpus(tmp_path)
    (tmp_path / "transcripts/unlisted.jsonl").write_bytes(b"{}\n")
    with pytest.raises(ManifestRefusalError, match="unindexed transcript"):
        HandlerTranscriptManifestSource(environ=env).handle(
            ModelTranscriptManifestSourceRequest()
        )


def test_source_refuses_a_symlink_outside_the_frozen_root(tmp_path: Path) -> None:
    env = frozen_corpus(tmp_path / "replay")
    original = tmp_path / "replay/transcripts/one.jsonl"
    outside = tmp_path / "live.jsonl"
    outside.write_bytes(original.read_bytes())
    original.unlink()
    original.symlink_to(outside)
    with pytest.raises(ManifestRefusalError, match="outside"):
        HandlerTranscriptManifestSource(environ=env).handle(
            ModelTranscriptManifestSourceRequest()
        )


@pytest.mark.asyncio
async def test_operator_node_dispatches_over_the_local_event_bus(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level("INFO")
    terminal_topic = "onex.evt.omnimarket.transcript-manifest-completed.v1"
    contract = yaml.safe_load(NodeTranscriptManifestEffect.contract_path.read_text())
    assert contract["terminal_event"] == terminal_topic
    assert terminal_topic in contract["event_bus"]["publish_topics"]
    env = frozen_corpus(tmp_path / "replay")
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    runtime = RuntimeLocal(
        workflow_path=NodeTranscriptManifestEffect.contract_path,
        state_root=tmp_path / "replay/state",
        backend_overrides={"event_bus": "inmemory"},
        timeout=10,
    )
    assert await runtime.run_async() is EnumWorkflowResult.COMPLETED
    assert any(
        f"LocalRuntimeBusAdapter: published to {terminal_topic} " in record.getMessage()
        for record in caplog.records
    )
    result = runtime.handler_result
    assert result is not None
    assert result.cases == 2
    assert result.boundaries == 3
