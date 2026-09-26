# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Declared output files: path rule, models and derived response contract (OMN-19600)."""

from __future__ import annotations

import jsonschema
import pytest
from pydantic import ValidationError

from omnimarket.delegation.declared_outputs_contract import (
    build_declared_outputs_response_contract,
    render_declared_outputs_instruction,
)
from omnimarket.delegation.output_path_safety import check_relative_output_path
from omnimarket.models.delegation.wire.model_delegation_output_files import (
    EnumDelegationOutputKind,
    ModelDeclaredOutputFile,
    ModelDeclaredOutputs,
    ModelDelegationOutputManifest,
    ModelDelegationOutputManifestEntry,
    compute_manifest_sha256,
)

pytestmark = pytest.mark.unit

_HEX = "a" * 64


@pytest.mark.parametrize(
    "path",
    [
        "src/slug.py",
        "tests/test_slug.py",
        "README.md",
        "a/b/c/d.txt",
        ".github/workflows/ci.yml",
    ],
)
def test_a_plain_relative_path_is_accepted(path: str) -> None:
    assert check_relative_output_path(path) == path


@pytest.mark.parametrize(
    "path",
    [
        "",
        " src/x.py",
        "/etc/passwd",
        "~/x.py",
        "../x.py",
        "src/../../x.py",
        "./x.py",
        "src//x.py",
        "src/",
        ".git/config",
        "sub/.git/hooks/pre-commit",
        ".git",
        "src\\x.py",
        "src/x\x00.py",
        "src/x\n.py",
        "C:/x.py",
        "/".join(["d"] * 17) + "/x.py",
        "x" * 256,
    ],
)
def test_an_unsafe_path_is_refused(path: str) -> None:
    with pytest.raises(ValueError, match="output path"):
        check_relative_output_path(path)


def test_declared_outputs_refuse_duplicate_and_case_fold_collision() -> None:
    with pytest.raises(ValidationError, match="duplicate"):
        ModelDeclaredOutputs(
            files=(
                ModelDeclaredOutputFile(path="src/a.py", kind="code"),
                ModelDeclaredOutputFile(path="src/a.py", kind="test"),
            )
        )
    with pytest.raises(ValidationError, match="case"):
        ModelDeclaredOutputs(
            files=(
                ModelDeclaredOutputFile(path="src/A.py", kind="code"),
                ModelDeclaredOutputFile(path="src/a.py", kind="code"),
            )
        )


def test_declared_output_refuses_an_unsafe_path_and_an_oversize_cap() -> None:
    with pytest.raises(ValidationError):
        ModelDeclaredOutputFile(path="../x.py", kind="code")
    with pytest.raises(ValidationError):
        ModelDeclaredOutputFile(path="x.py", kind="code", max_bytes=262_145)


def test_manifest_digest_is_order_independent_and_checked() -> None:
    first = ModelDelegationOutputManifestEntry(
        path="b.py",
        kind=EnumDelegationOutputKind.CODE,
        media_type="text/x-python",
        sha256=_HEX,
        size_bytes=1,
        artifact_ref=f"sha256:{_HEX}",
    )
    second = first.model_copy(update={"path": "a.py", "sha256": "b" * 64})
    second = second.model_copy(update={"artifact_ref": f"sha256:{'b' * 64}"})
    assert compute_manifest_sha256((first, second)) == compute_manifest_sha256(
        (second, first)
    )
    manifest = ModelDelegationOutputManifest(
        entries=(first, second),
        refusals=(),
        manifest_sha256=compute_manifest_sha256((first, second)),
    )
    assert len(manifest.entries) == 2
    with pytest.raises(ValidationError, match="manifest_sha256"):
        ModelDelegationOutputManifest(
            entries=(first,), refusals=(), manifest_sha256="0" * 64
        )


def test_manifest_entry_artifact_ref_must_equal_its_sha256() -> None:
    with pytest.raises(ValidationError, match="artifact_ref"):
        ModelDelegationOutputManifestEntry(
            path="a.py",
            kind=EnumDelegationOutputKind.CODE,
            media_type="text/plain",
            sha256=_HEX,
            size_bytes=1,
            artifact_ref=f"sha256:{'b' * 64}",
        )


def test_derived_contract_accepts_the_declared_shape_and_refuses_others() -> None:
    declared = ModelDeclaredOutputs(
        files=(
            ModelDeclaredOutputFile(path="src/slug.py", kind="code"),
            ModelDeclaredOutputFile(path="tests/test_slug.py", kind="test"),
        )
    )
    schema = build_declared_outputs_response_contract(declared)
    jsonschema.validators.validator_for(schema).check_schema(schema)
    good = {
        "files": [
            {"path": "src/slug.py", "content": "x = 1\n"},
            {"path": "tests/test_slug.py", "content": "def test(): pass\n"},
        ]
    }
    jsonschema.validate(good, schema)
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(
            {"files": [{"path": "evil.py", "content": ""}, *good["files"][:1]]},
            schema,
        )
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate({"files": good["files"][:1]}, schema)
    instruction = render_declared_outputs_instruction(declared)
    assert "src/slug.py" in instruction
    assert "tests/test_slug.py" in instruction
    assert '"files"' in instruction
