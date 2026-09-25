# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The materialize effect writes declared files into the target and the store (OMN-19600)."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from uuid import UUID

import pytest
from omnibase_core.artifacts.artifact_store import ArtifactStore
from omnibase_core.models.artifacts.model_artifact_ref import ModelArtifactRef

from omnimarket.models.delegation.wire.model_delegation_output_files import (
    EnumDelegationOutputFileRefusalReason,
    ModelDeclaredOutputFile,
    ModelDeclaredOutputs,
    ModelDelegationOutputFile,
    ModelDelegationOutputManifest,
    ModelDelegationOutputManifestEntry,
    compute_manifest_sha256,
)
from omnimarket.nodes.node_delegation_output_extract_compute import (
    HandlerDelegationOutputExtract,
    ModelDelegationOutputExtractRequest,
)
from omnimarket.nodes.node_delegation_output_materialize_effect import (
    HandlerDelegationOutputMaterialize,
    ModelDelegationOutputMaterializeRequest,
)

pytestmark = pytest.mark.unit

_R = EnumDelegationOutputFileRefusalReason
_CORRELATION = UUID("00000000-0000-4000-8000-000000019600")


@pytest.fixture
def store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> ArtifactStore:
    monkeypatch.setenv("ONEX_ARTIFACT_STORE_ROOT", str(tmp_path / "artifacts"))
    return ArtifactStore()


def _extracted(*items: tuple[str, str, str]):  # type: ignore[no-untyped-def]
    declared = ModelDeclaredOutputs(
        files=tuple(ModelDeclaredOutputFile(path=p, kind=k) for p, k, _ in items)
    )
    reply = json.dumps({"files": [{"path": p, "content": c} for p, _, c in items]})
    return HandlerDelegationOutputExtract().handle(
        ModelDelegationOutputExtractRequest(
            response_text=reply, declared_outputs=declared
        )
    )


def _request(target: Path, extracted, **kwargs):  # type: ignore[no-untyped-def]
    return ModelDelegationOutputMaterializeRequest(
        correlation_id=_CORRELATION,
        target_root=str(target),
        files=extracted.files,
        manifest=extracted.manifest,
        **kwargs,
    )


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def test_declared_files_land_in_the_target_and_the_store_with_one_hash(
    tmp_path: Path, store: ArtifactStore
) -> None:
    target = tmp_path / "target"
    extracted = _extracted(
        ("src/slug.py", "code", "def slugify(t):\n    return t\n"),
        ("tests/test_slug.py", "test", "def test_x():\n    pass\n"),
    )
    result = HandlerDelegationOutputMaterialize(artifact_store=store).handle(
        _request(target, extracted)
    )

    assert result.refusals == ()
    assert [w.path for w in result.written] == ["src/slug.py", "tests/test_slug.py"]
    for written, entry in zip(result.written, extracted.manifest.entries, strict=True):
        on_disk = (target / written.path).read_bytes()
        assert _sha(on_disk) == entry.sha256 == written.on_disk_sha256
        blob = store.read_blob(ModelArtifactRef(ref=entry.artifact_ref))
        assert _sha(blob) == entry.sha256
        assert written.artifact_ref == entry.artifact_ref
        mode = (target / written.path).stat().st_mode
        assert not mode & 0o111
    assert result.manifest_sha256 == extracted.manifest.manifest_sha256


def test_replay_is_idempotent_on_the_file_and_the_artifact(
    tmp_path: Path, store: ArtifactStore
) -> None:
    target = tmp_path / "target"
    extracted = _extracted(("a.txt", "doc", "hello\n"))
    handler = HandlerDelegationOutputMaterialize(artifact_store=store)
    first = handler.handle(_request(target, extracted))
    second = handler.handle(_request(target, extracted))
    assert first.written[0].created is True
    assert second.written[0].created is False
    assert second.refusals == ()
    assert second.written[0].on_disk_sha256 == first.written[0].on_disk_sha256
    blobs = [p for p in (tmp_path / "artifacts").rglob("*") if p.is_file()]
    assert len([p for p in blobs if not p.name.endswith(".meta.json")]) == 1


def test_idempotent_replay_refuses_an_existing_file_with_different_bytes(
    tmp_path: Path, store: ArtifactStore
) -> None:
    target = tmp_path / "target"
    (target).mkdir()
    (target / "a.txt").write_text("someone else's file\n")
    extracted = _extracted(("a.txt", "doc", "hello\n"))
    result = HandlerDelegationOutputMaterialize(artifact_store=store).handle(
        _request(target, extracted)
    )
    assert result.written == ()
    assert [(r.path, r.reason) for r in result.refusals] == [
        ("a.txt", _R.EXISTS_WITH_DIFFERENT_CONTENT)
    ]
    assert (target / "a.txt").read_text() == "someone else's file\n"

    replaced = HandlerDelegationOutputMaterialize(artifact_store=store).handle(
        _request(target, extracted, overwrite="replace_declared")
    )
    assert replaced.refusals == ()
    assert (target / "a.txt").read_text() == "hello\n"


def test_a_symlinked_parent_directory_is_refused_and_nothing_escapes(
    tmp_path: Path, store: ArtifactStore
) -> None:
    target = tmp_path / "target"
    outside = tmp_path / "outside"
    target.mkdir()
    outside.mkdir()
    os.symlink(outside, target / "tests")
    extracted = _extracted(
        ("tests/test_x.py", "test", "def test_x():\n    pass\n"),
        ("src/x.py", "code", "x = 1\n"),
    )
    result = HandlerDelegationOutputMaterialize(artifact_store=store).handle(
        _request(target, extracted)
    )
    assert [w.path for w in result.written] == ["src/x.py"]
    assert [(r.path, r.reason) for r in result.refusals] == [
        ("tests/test_x.py", _R.SYMLINK_IN_TARGET)
    ]
    assert list(outside.iterdir()) == []


def test_a_symlink_at_the_final_name_is_refused_and_not_followed(
    tmp_path: Path, store: ArtifactStore
) -> None:
    target = tmp_path / "target"
    outside = tmp_path / "outside.txt"
    target.mkdir()
    outside.write_text("keep\n")
    os.symlink(outside, target / "a.txt")
    extracted = _extracted(("a.txt", "doc", "hello\n"))
    result = HandlerDelegationOutputMaterialize(artifact_store=store).handle(
        _request(target, extracted, overwrite="replace_declared")
    )
    assert [(r.path, r.reason) for r in result.refusals] == [
        ("a.txt", _R.SYMLINK_IN_TARGET)
    ]
    assert outside.read_text() == "keep\n"


def test_a_symlinked_target_root_is_refused(
    tmp_path: Path, store: ArtifactStore
) -> None:
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    os.symlink(real, link)
    extracted = _extracted(("a.txt", "doc", "hello\n"))
    with pytest.raises(ValueError, match="symlink"):
        HandlerDelegationOutputMaterialize(artifact_store=store).handle(
            _request(link, extracted)
        )
    assert list(real.iterdir()) == []


def test_a_relative_target_root_is_refused(store: ArtifactStore) -> None:
    extracted = _extracted(("a.txt", "doc", "hello\n"))
    with pytest.raises(ValueError, match="absolute"):
        _request(Path("relative/target"), extracted)


@pytest.mark.parametrize("path", ["../escape.txt", "/abs.txt", ".git/config"])
def test_an_unsafe_path_cannot_even_reach_the_effect(path: str) -> None:
    with pytest.raises(ValueError, match="output path"):
        ModelDelegationOutputFile(path=path, kind="doc", content="x")


def test_a_file_whose_bytes_differ_from_its_manifest_entry_is_refused(
    tmp_path: Path, store: ArtifactStore
) -> None:
    target = tmp_path / "target"
    data = b"hello\n"
    entry = ModelDelegationOutputManifestEntry(
        path="a.txt",
        kind="doc",
        media_type="text/plain",
        sha256=_sha(data),
        size_bytes=len(data),
        artifact_ref=f"sha256:{_sha(data)}",
    )
    manifest = ModelDelegationOutputManifest(
        entries=(entry,), refusals=(), manifest_sha256=compute_manifest_sha256((entry,))
    )
    tampered = ModelDelegationOutputFile(path="a.txt", kind="doc", content="evil\n")
    result = HandlerDelegationOutputMaterialize(artifact_store=store).handle(
        ModelDelegationOutputMaterializeRequest(
            correlation_id=_CORRELATION,
            target_root=str(target),
            files=(tampered,),
            manifest=manifest,
        )
    )
    assert result.written == ()
    assert [(r.path, r.reason) for r in result.refusals] == [
        ("a.txt", _R.MANIFEST_MISMATCH)
    ]
    assert not (target / "a.txt").exists()


def test_a_secret_the_store_detects_is_refused_and_not_written(
    tmp_path: Path, store: ArtifactStore
) -> None:
    target = tmp_path / "target"
    secret = "ghp_" + "B" * 36 + "\n"
    data = secret.encode()
    entry = ModelDelegationOutputManifestEntry(
        path="a.txt",
        kind="doc",
        media_type="text/plain",
        sha256=_sha(data),
        size_bytes=len(data),
        artifact_ref=f"sha256:{_sha(data)}",
    )
    manifest = ModelDelegationOutputManifest(
        entries=(entry,), refusals=(), manifest_sha256=compute_manifest_sha256((entry,))
    )
    result = HandlerDelegationOutputMaterialize(artifact_store=store).handle(
        ModelDelegationOutputMaterializeRequest(
            correlation_id=_CORRELATION,
            target_root=str(target),
            files=(
                ModelDelegationOutputFile(path="a.txt", kind="doc", content=secret),
            ),
            manifest=manifest,
        )
    )
    assert result.written == ()
    assert [(r.path, r.reason) for r in result.refusals] == [
        ("a.txt", _R.SECRET_DETECTED)
    ]
    assert not (target / "a.txt").exists()


def test_extract_refusals_are_carried_into_the_result(
    tmp_path: Path, store: ArtifactStore
) -> None:
    declared = ModelDeclaredOutputs(
        files=(ModelDeclaredOutputFile(path="a.txt", kind="doc"),)
    )
    reply = json.dumps(
        {
            "files": [
                {"path": "a.txt", "content": "x"},
                {"path": "b.txt", "content": "y"},
            ]
        }
    )
    extracted = HandlerDelegationOutputExtract().handle(
        ModelDelegationOutputExtractRequest(
            response_text=reply, declared_outputs=declared
        )
    )
    result = HandlerDelegationOutputMaterialize(artifact_store=store).handle(
        _request(tmp_path / "target", extracted)
    )
    assert [w.path for w in result.written] == ["a.txt"]
    assert [(r.path, r.reason) for r in result.refusals] == [
        ("b.txt", _R.UNDECLARED_PATH)
    ]
