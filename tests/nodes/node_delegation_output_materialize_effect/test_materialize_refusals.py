# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Manifest and filesystem refusal paths leave unrelated files untouched."""

import errno
import hashlib
import os
from pathlib import Path
from uuid import UUID

import pytest
from omnibase_core.artifacts.artifact_store import ArtifactStore
from omnibase_core.errors.error_artifact_quota_exceeded import (
    ArtifactQuotaExceededError,
)
from omnibase_core.models.artifacts.model_artifact_ref import ModelArtifactRef

from omnimarket.models.delegation.wire.model_delegation_output_files import (
    EnumDelegationOutputFileRefusalReason as Reason,
)
from omnimarket.models.delegation.wire.model_delegation_output_files import (
    ModelDelegationOutputFile,
    ModelDelegationOutputManifest,
    ModelDelegationOutputManifestEntry,
    compute_manifest_sha256,
)
from omnimarket.nodes.node_delegation_output_materialize_effect import (
    HandlerDelegationOutputMaterialize,
    ModelDelegationOutputMaterializeRequest,
)

pytestmark = pytest.mark.unit


def _request(
    target: Path, content: str = "hello", path: str = "a.txt"
) -> ModelDelegationOutputMaterializeRequest:
    digest = hashlib.sha256(content.encode()).hexdigest()
    entry = ModelDelegationOutputManifestEntry(
        path=path,
        kind="doc",
        media_type="text/plain",
        sha256=digest,
        size_bytes=len(content.encode()),
        artifact_ref=f"sha256:{digest}",
    )
    return ModelDelegationOutputMaterializeRequest(
        correlation_id=UUID(int=1),
        target_root=str(target),
        files=(ModelDelegationOutputFile(path=path, kind="doc", content=content),),
        manifest=ModelDelegationOutputManifest(
            entries=(entry,),
            manifest_sha256=compute_manifest_sha256((entry,)),
        ),
    )


@pytest.fixture
def store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> ArtifactStore:
    monkeypatch.setenv("ONEX_ARTIFACT_STORE_ROOT", str(tmp_path / "artifacts"))
    return ArtifactStore()


@pytest.mark.parametrize("mismatch", ["missing-entry", "size", "missing-file"])
def test_manifest_disagreement_refuses_before_target_write(
    tmp_path: Path,
    store: ArtifactStore,
    mismatch: str,
) -> None:
    request = _request(tmp_path / "target")
    entries = request.manifest.entries
    if mismatch == "missing-entry":
        entries = ()
    elif mismatch == "size":
        entries = (entries[0].model_copy(update={"size_bytes": 6}),)
    request = request.model_copy(
        update={
            "files": () if mismatch == "missing-file" else request.files,
            "manifest": ModelDelegationOutputManifest(
                entries=entries,
                manifest_sha256=compute_manifest_sha256(entries),
            ),
        }
    )
    result = HandlerDelegationOutputMaterialize(store).handle(request)
    assert result.written == ()
    assert [(r.path, r.reason) for r in result.refusals] == [
        (
            "a.txt",
            Reason.MISSING if mismatch == "missing-file" else Reason.MANIFEST_MISMATCH,
        )
    ]
    assert list((tmp_path / "target").iterdir()) == []
    assert result.manifest_sha256 == compute_manifest_sha256(())


@pytest.mark.parametrize("failure", ["quota", "stored-ref"])
def test_store_refusals_do_not_write_target(
    tmp_path: Path,
    store: ArtifactStore,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    def write_blob(*args: object, **kwargs: object) -> ModelArtifactRef:
        if failure == "quota":
            raise ArtifactQuotaExceededError("test quota")
        return ModelArtifactRef(ref="sha256:" + "0" * 64)

    monkeypatch.setattr(store, "write_blob", write_blob)
    result = HandlerDelegationOutputMaterialize(store).handle(
        _request(tmp_path / "target")
    )
    assert result.written == ()
    assert [(r.path, r.reason) for r in result.refusals] == [
        (
            "a.txt",
            Reason.TOO_LARGE if failure == "quota" else Reason.MANIFEST_MISMATCH,
        )
    ]
    assert list((tmp_path / "target").iterdir()) == []


def test_directory_at_final_name_is_preserved_even_when_replace_is_declared(
    tmp_path: Path,
    store: ArtifactStore,
) -> None:
    target = tmp_path / "target"
    (target / "a.txt").mkdir(parents=True)
    (target / "a.txt" / "keep").write_text("untouched")
    request = _request(target).model_copy(update={"overwrite": "replace_declared"})
    result = HandlerDelegationOutputMaterialize(store).handle(request)
    assert result.written == ()
    assert result.refusals[0].reason == Reason.EXISTS_WITH_DIFFERENT_CONTENT
    assert (target / "a.txt" / "keep").read_text() == "untouched"


def test_nested_link_outside_declared_root_refuses_without_changing_outside(
    tmp_path: Path,
    store: ArtifactStore,
) -> None:
    target = tmp_path / "target"
    outside = tmp_path / "outside"
    (target / "nested").mkdir(parents=True)
    outside.mkdir()
    (outside / "a.txt").write_text("keep")
    (target / "nested" / "link").symlink_to(outside, target_is_directory=True)
    request = _request(target, path="nested/link/a.txt").model_copy(
        update={"overwrite": "replace_declared"}
    )
    result = HandlerDelegationOutputMaterialize(store).handle(request)
    assert result.written == ()
    assert [(r.path, r.reason) for r in result.refusals] == [
        (
            "nested/link/a.txt",
            Reason.SYMLINK_IN_TARGET,
        )
    ]
    assert (outside / "a.txt").read_text() == "keep"
    assert sorted(p.name for p in outside.iterdir()) == ["a.txt"]


@pytest.mark.parametrize("content", ["", "é" * 40000], ids=["empty", "multi-chunk"])
def test_lazy_store_handles_empty_and_multiple_read_chunks(
    tmp_path: Path,
    store: ArtifactStore,
    content: str,
) -> None:
    request = _request(tmp_path / "target", content)
    handler = HandlerDelegationOutputMaterialize()
    result = handler.handle(request)
    assert result.refusals == ()
    assert result.written[0].created is True
    assert result.written[0].size_bytes == len(content.encode())
    assert (tmp_path / "target" / "a.txt").read_bytes() == content.encode()
    assert (
        store.read_blob(ModelArtifactRef(ref=result.written[0].artifact_ref))
        == content.encode()
    )
    assert handler.handle(request).written[0].created is False


def test_disk_bytes_changed_after_placement_are_refused(
    tmp_path: Path,
    store: ArtifactStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "target"
    original = HandlerDelegationOutputMaterialize._place

    def place(name: str, data: bytes, digest: str, dir_fd: int, overwrite: str) -> bool:
        created = original(name, data, digest, dir_fd, overwrite)
        (target / name).write_bytes(b"changed")
        return created

    monkeypatch.setattr(
        HandlerDelegationOutputMaterialize, "_place", staticmethod(place)
    )
    result = HandlerDelegationOutputMaterialize(store).handle(_request(target))
    assert result.written == ()
    assert result.refusals[0].reason == Reason.MANIFEST_MISMATCH
    assert result.refusals[0].detail == "file on disk differs after the write"


@pytest.mark.parametrize(
    ("error", "message"),
    [
        (errno.ELOOP, "target_root is a symlink"),
        (errno.EMLINK, "target_root is a symlink"),
        (errno.EACCES, "target_root is not an openable directory"),
    ],
)
def test_root_open_errors_refuse_the_whole_request(
    tmp_path: Path,
    store: ArtifactStore,
    monkeypatch: pytest.MonkeyPatch,
    error: int,
    message: str,
) -> None:
    target = tmp_path / "target"
    target.mkdir()

    def fail(name: str, flags: int) -> int:
        assert name == str(target)
        raise OSError(error, "injected root open failure")

    monkeypatch.setattr(os, "open", fail)
    with pytest.raises(ValueError, match=message):
        HandlerDelegationOutputMaterialize(store).handle(_request(target))
    assert list(target.iterdir()) == []
