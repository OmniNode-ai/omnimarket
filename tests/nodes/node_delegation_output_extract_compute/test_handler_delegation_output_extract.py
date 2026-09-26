# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The pure extract compute turns an accepted reply into files and a manifest (OMN-19600)."""

from __future__ import annotations

import hashlib
import json

import pytest

from omnimarket.models.delegation.wire.model_delegation_output_files import (
    EnumDelegationOutputFileRefusalReason,
    ModelDeclaredOutputFile,
    ModelDeclaredOutputs,
    compute_manifest_sha256,
)
from omnimarket.nodes.node_delegation_output_extract_compute import (
    HandlerDelegationOutputExtract,
    ModelDelegationOutputExtractRequest,
)

pytestmark = pytest.mark.unit

_SRC = "def slugify(text: str) -> str:\n    return text.lower().replace(' ', '-')\n"
_TEST = "from src.slug import slugify\n\n\ndef test_slugify() -> None:\n    assert slugify('A B') == 'a-b'\n"


def _declared(
    *files: tuple[str, str], max_bytes: int = 262_144
) -> ModelDeclaredOutputs:
    return ModelDeclaredOutputs(
        files=tuple(
            ModelDeclaredOutputFile(path=path, kind=kind, max_bytes=max_bytes)
            for path, kind in files
        )
    )


def _reply(*items: tuple[str, str]) -> str:
    return json.dumps({"files": [{"path": p, "content": c} for p, c in items]})


def _run(reply: str, declared: ModelDeclaredOutputs):  # type: ignore[no-untyped-def]
    return HandlerDelegationOutputExtract().handle(
        ModelDelegationOutputExtractRequest(
            response_text=reply, declared_outputs=declared
        )
    )


def test_two_declared_files_become_two_entries_with_matching_hashes() -> None:
    declared = _declared(("src/slug.py", "code"), ("tests/test_slug.py", "test"))
    result = _run(
        _reply(("src/slug.py", _SRC), ("tests/test_slug.py", _TEST)), declared
    )

    assert [f.path for f in result.files] == ["src/slug.py", "tests/test_slug.py"]
    assert result.manifest.refusals == ()
    for file, entry in zip(result.files, result.manifest.entries, strict=True):
        data = file.content.encode("utf-8")
        assert entry.path == file.path
        assert entry.sha256 == hashlib.sha256(data).hexdigest()
        assert entry.size_bytes == len(data)
        assert entry.artifact_ref == f"sha256:{entry.sha256}"
    assert result.manifest.manifest_sha256 == compute_manifest_sha256(
        result.manifest.entries
    )
    assert [e.kind.value for e in result.manifest.entries] == ["code", "test"]


def test_a_fenced_reply_with_a_preamble_is_still_extracted() -> None:
    declared = _declared(("src/slug.py", "code"))
    reply = "Here you go:\n```json\n" + _reply(("src/slug.py", _SRC)) + "\n```\n"
    result = _run(reply, declared)
    assert [f.content for f in result.files] == [_SRC]


def test_an_undeclared_path_is_refused_and_the_declared_one_kept() -> None:
    declared = _declared(("src/slug.py", "code"))
    result = _run(_reply(("src/slug.py", _SRC), ("src/evil.py", "x")), declared)
    assert [f.path for f in result.files] == ["src/slug.py"]
    assert [(r.path, r.reason) for r in result.manifest.refusals] == [
        ("src/evil.py", EnumDelegationOutputFileRefusalReason.UNDECLARED_PATH)
    ]


@pytest.mark.parametrize("path", ["../escape.py", "/etc/passwd", ".git/config"])
def test_an_unsafe_path_is_refused_as_unsafe(path: str) -> None:
    declared = _declared(("src/slug.py", "code"))
    result = _run(_reply(("src/slug.py", _SRC), (path, "x")), declared)
    assert [f.path for f in result.files] == ["src/slug.py"]
    assert [(r.path, r.reason) for r in result.manifest.refusals] == [
        (path, EnumDelegationOutputFileRefusalReason.PATH_UNSAFE)
    ]


def test_an_oversize_file_is_refused() -> None:
    declared = _declared(("src/slug.py", "code"), max_bytes=10)
    result = _run(_reply(("src/slug.py", _SRC)), declared)
    assert result.files == ()
    reasons = {r.reason for r in result.manifest.refusals}
    assert EnumDelegationOutputFileRefusalReason.TOO_LARGE in reasons


def test_a_total_over_the_declared_bound_refuses_the_file_that_crosses_it() -> None:
    declared = ModelDeclaredOutputs(
        files=(
            ModelDeclaredOutputFile(path="a.txt", kind="doc"),
            ModelDeclaredOutputFile(path="b.txt", kind="doc"),
        ),
        max_total_bytes=15,
    )
    result = _run(_reply(("a.txt", "x" * 10), ("b.txt", "y" * 10)), declared)
    assert [f.path for f in result.files] == ["a.txt"]
    assert [(r.path, r.reason) for r in result.manifest.refusals] == [
        ("b.txt", EnumDelegationOutputFileRefusalReason.TOTAL_TOO_LARGE)
    ]


def test_a_secret_bearing_file_is_refused_and_its_content_is_not_kept() -> None:
    declared = _declared(("src/config.py", "code"))
    secret = "TOKEN = 'ghp_" + "A" * 36 + "'\n"
    result = _run(_reply(("src/config.py", secret)), declared)
    assert result.files == ()
    (refusal,) = result.manifest.refusals
    assert refusal.reason is EnumDelegationOutputFileRefusalReason.SECRET_DETECTED
    assert "ghp_" not in refusal.detail


def test_a_duplicate_and_a_missing_file_are_both_named() -> None:
    declared = _declared(("a.txt", "doc"), ("b.txt", "doc"))
    result = _run(_reply(("a.txt", "one"), ("a.txt", "two")), declared)
    assert [f.content for f in result.files] == ["one"]
    assert [(r.path, r.reason) for r in result.manifest.refusals] == [
        ("a.txt", EnumDelegationOutputFileRefusalReason.DUPLICATE),
        ("b.txt", EnumDelegationOutputFileRefusalReason.MISSING),
    ]


def test_a_reply_with_no_files_object_is_one_whole_reply_refusal() -> None:
    declared = _declared(("src/slug.py", "code"))
    result = _run("```python\n# src/slug.py\nprint(1)\n```", declared)
    assert result.files == ()
    assert result.manifest.entries == ()
    assert result.manifest.refusals[0].path is None
    assert (
        result.manifest.refusals[0].reason
        is EnumDelegationOutputFileRefusalReason.NO_FILES_OBJECT
    )


def test_the_same_reply_gives_the_same_manifest() -> None:
    declared = _declared(("src/slug.py", "code"))
    reply = _reply(("src/slug.py", _SRC))
    assert _run(reply, declared) == _run(reply, declared)
