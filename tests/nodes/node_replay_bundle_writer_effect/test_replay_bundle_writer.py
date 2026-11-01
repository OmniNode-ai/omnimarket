# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""node_replay_bundle_writer_effect writes under the overlay-declared root and re-hashes (OMN-21108)."""

from __future__ import annotations

import importlib
import posixpath
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import yaml

from omnimarket.nodes.node_context_compaction_manifest_compute import models as cm
from omnimarket.nodes.node_context_compaction_manifest_compute.handlers.handler_compaction_manifest import (
    HandlerReplayManifestBuilder,
)
from omnimarket.nodes.node_replay_bundle_writer_effect.handlers.handler_replay_bundle_writer import (
    OVERLAY_SCHEMA,
    HandlerReplayBundleWriter,
)
from omnimarket.nodes.node_replay_bundle_writer_effect.models import (
    EnumReplayBundleWriteStatus,
    ModelReplayBundleWriteRequest,
)

pytestmark = pytest.mark.unit

ENV = {"OMNI_HOME": "/omni", "HOME": "/home/x"}
OVERLAY = "/overlay/replay_root.yaml"


class MemoryFilesystem:
    """In-memory ProtocolReplayBundleFilesystem; optionally corrupts one path on write."""

    def __init__(self, files: dict[str, bytes], corrupt: str | None = None) -> None:
        self.files = dict(files)
        self.corrupt = corrupt

    def resolve(self, path: str) -> str:
        return posixpath.normpath(path)

    def write_bytes(self, path: str, data: bytes) -> None:
        self.files[path] = data + b"X" if path == self.corrupt else data

    def read_bytes(self, path: str) -> bytes | None:
        return self.files.get(path)


def _overlay(root: str | None, host: str = "h200") -> bytes:
    entry: dict[str, str] = {} if root is None else {"root": root}
    return yaml.safe_dump({"schema": OVERLAY_SCHEMA, "hosts": {host: entry}}).encode()


def _t(minute: int) -> datetime:
    return datetime(2025, 2, 15, 0, minute, tzinfo=UTC)


def _built() -> cm.ModelBuildManifestResult:
    words = "keep the retry budget"
    spec = cm.ModelCaseSpec(
        case_id="c",
        finding="f",
        census_status="CONFIRMED",
        census_ref="x",
        session_id="s",
        boundary_pre_event_ids=("pre",),
        expected_statements=(
            cm.ModelExpectedStatement(
                statement_id="S", prompt_event_id="op", required_spans=(words,)
            ),
        ),
        post_boundary_evidence=(
            cm.ModelPostBoundaryEvidence(
                event_id="late", kind="RESTATED", required_spans=("retry",)
            ),
        ),
        question="q",
        expected_answer="a",
    )
    pre = cm.ModelHookBoundaryEvent(
        event_id="pre",
        session_id="s",
        hook_event_name=cm.EnumBoundaryHook.PRE,
        emitted_at=_t(40),
        trigger="manual",
    )

    def rec(i: str, kind: str, minute: int, content: str) -> cm.ModelCapturedRecord:
        return cm.ModelCapturedRecord(
            event_id=i,
            session_id="s",
            content_kind=kind,
            emitted_at=_t(minute),
            content=content,
        )

    return HandlerReplayManifestBuilder().handle(
        cm.ModelBuildManifestRequest(
            specs=(spec,),
            events=(pre,),
            records=(
                rec("op", "operator_prompt", 10, words),
                rec("late", "prompt", 50, "retry again"),
            ),
            ledger_files=(
                cm.ModelLedgerFile(
                    name="l.md", content=b"2025-02-15T00:01:00Z | STATUS | lane=a | x\n"
                ),
            ),
            census_ref="x",
        )
    )


def _request(built: cm.ModelBuildManifestResult) -> ModelReplayBundleWriteRequest:
    return ModelReplayBundleWriteRequest(
        manifest_yaml=built.manifest_yaml,
        bundle_files=built.bundle_files,
        overlay_path=OVERLAY,
        host_id="h200",
    )


def test_writes_every_file_under_the_declared_root_and_verifies() -> None:
    built = _built()
    fs = MemoryFilesystem({OVERLAY: _overlay("$OMNI_HOME/replay")})
    result = HandlerReplayBundleWriter(fs, ENV).handle(_request(built))
    assert result.status is EnumReplayBundleWriteStatus.WRITTEN
    assert result.root == "/omni/replay"
    assert result.cases_verified == 1
    assert len(result.files) == len(built.bundle_files) > 0
    assert all(f.verified for f in result.files)
    for file in built.bundle_files:
        assert fs.files[f"/omni/replay/{file.path}"] == file.content


def test_corrupted_file_is_refused_with_hash_mismatch() -> None:
    built = _built()
    victim = built.bundle_files[0].path
    fs = MemoryFilesystem(
        {OVERLAY: _overlay("$OMNI_HOME/replay")}, corrupt=f"/omni/replay/{victim}"
    )
    result = HandlerReplayBundleWriter(fs, ENV).handle(_request(built))
    assert result.status is EnumReplayBundleWriteStatus.REFUSED_HASH_MISMATCH
    assert result.cases_verified == 0
    assert any(victim in failure for failure in result.failures)
    assert not all(f.verified for f in result.files)


def test_pinned_hash_mismatch_is_refused_even_when_bytes_round_trip() -> None:
    built = _built()
    manifest = yaml.safe_load(built.manifest_yaml)
    manifest["cases"][0]["transcript_chain"]["sha256"] = "0" * 64
    request = _request(built).model_copy(
        update={"manifest_yaml": yaml.safe_dump(manifest)}
    )
    fs = MemoryFilesystem({OVERLAY: _overlay("$OMNI_HOME/replay")})
    result = HandlerReplayBundleWriter(fs, ENV).handle(request)
    assert result.status is EnumReplayBundleWriteStatus.REFUSED_HASH_MISMATCH
    assert any("sha256 mismatch" in failure for failure in result.failures)


@pytest.mark.parametrize(
    "files",
    [
        {},  # no overlay file
        {OVERLAY: _overlay(None)},  # host entry without a root
        {OVERLAY: _overlay("")},  # empty root
        {OVERLAY: _overlay("$OMNI_HOME/replay", host="other")},  # host not declared
        {OVERLAY: _overlay("/absolute/not/expandable")},  # not under OMNI_HOME or HOME
    ],
)
def test_no_default_root_unset_is_a_typed_refusal(files: dict[str, bytes]) -> None:
    fs = MemoryFilesystem(files)
    result = HandlerReplayBundleWriter(fs, ENV).handle(_request(_built()))
    assert result.status is EnumReplayBundleWriteStatus.REFUSED_ROOT_UNDECLARED
    assert result.root is None
    assert set(fs.files) == set(files)


def test_unset_home_variable_is_a_typed_refusal() -> None:
    fs = MemoryFilesystem({OVERLAY: _overlay("$OMNI_HOME/replay")})
    result = HandlerReplayBundleWriter(fs, {}).handle(_request(_built()))
    assert result.status is EnumReplayBundleWriteStatus.REFUSED_ROOT_UNDECLARED


def test_path_escaping_the_root_is_refused_before_any_write() -> None:
    built = _built()
    escaping = built.bundle_files[0].model_copy(update={"path": "../escape.bin"})
    request = _request(built).model_copy(update={"bundle_files": (escaping,)})
    fs = MemoryFilesystem({OVERLAY: _overlay("$OMNI_HOME/replay")})
    result = HandlerReplayBundleWriter(fs, ENV).handle(request)
    assert result.status is EnumReplayBundleWriteStatus.REFUSED_OUTSIDE_ROOT
    assert set(fs.files) == {OVERLAY}


NODE_DIR = (
    Path(__file__).resolve().parents[3]
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_replay_bundle_writer_effect"
)


def test_the_contract_declares_topics_handler_and_no_env_default() -> None:
    contract: dict[str, Any] = yaml.safe_load(
        (NODE_DIR / "contract.yaml").read_text(encoding="utf-8")
    )
    command = "onex.cmd.omnimarket.replay-bundle-write-requested.v1"
    event = "onex.evt.omnimarket.replay-bundle-write-completed.v1"
    assert contract["event_bus"]["subscribe_topics"] == [command]
    assert contract["event_bus"]["publish_topics"] == [event]
    assert contract["terminal_event"] == event
    assert "required_env" not in contract
    handler = contract["handler"]
    cls = getattr(importlib.import_module(handler["module"]), handler["class"])
    assert callable(cls.handle)


def test_the_node_source_names_no_home_path_topic_or_env_read() -> None:
    for path in NODE_DIR.rglob("*.py"):
        body = path.read_text(encoding="utf-8")
        for needle in (
            "onex.cmd.",
            "onex.evt.",
            "home()",
            "expanduser",
            "~/",
        ):
            assert needle not in body, (path, needle)
