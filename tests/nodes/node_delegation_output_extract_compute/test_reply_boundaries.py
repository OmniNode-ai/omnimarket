# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Reply boundaries and typed outputs of the declared-file extractor."""

from __future__ import annotations

import hashlib
import json

import pytest
from omnibase_core.models.delegation.wire import EnumDelegationOutputShape

from omnimarket.delegation.deliverable_extraction import (
    ModelDeliverableContract,
    resolve_deliverable_contract,
)
from omnimarket.models.delegation.wire.model_delegation_output_files import (
    EnumDelegationOutputFileRefusalReason,
    EnumDelegationOutputKind,
    ModelDeclaredOutputFile,
    ModelDeclaredOutputs,
    ModelDelegationOutputFile,
    ModelDelegationOutputFileRefusal,
    ModelDelegationOutputManifest,
    ModelDelegationOutputManifestEntry,
)
from omnimarket.nodes.node_delegation_output_extract_compute import (
    HandlerDelegationOutputExtract,
    ModelDelegationOutputExtractRequest,
    ModelDelegationOutputExtractResult,
)

pytestmark = pytest.mark.unit

_PATH = "src/answer.py"
_CODE = "print('answer')\n"


def _reply(content: str = _CODE) -> str:
    return json.dumps({"files": [{"path": _PATH, "content": content}]})


def _run(
    reply: str,
    *,
    kind: EnumDelegationOutputKind = EnumDelegationOutputKind.CODE,
    max_bytes: int = 262_144,
    max_total_bytes: int = 1_048_576,
) -> ModelDelegationOutputExtractResult:
    return HandlerDelegationOutputExtract().handle(
        ModelDelegationOutputExtractRequest(
            response_text=reply,
            declared_outputs=ModelDeclaredOutputs(
                files=(
                    ModelDeclaredOutputFile(
                        path=_PATH,
                        kind=kind,
                        media_type="text/x-python",
                        max_bytes=max_bytes,
                    ),
                ),
                max_total_bytes=max_total_bytes,
            ),
        )
    )


def _accepted(
    content: str = _CODE,
    kind: EnumDelegationOutputKind = EnumDelegationOutputKind.CODE,
) -> ModelDelegationOutputExtractResult:
    data = content.encode("utf-8")
    digest = hashlib.sha256(data).hexdigest()
    manifest_digest = hashlib.sha256(f"{_PATH}\0{digest}".encode()).hexdigest()
    return ModelDelegationOutputExtractResult(
        files=(
            ModelDelegationOutputFile(
                path=_PATH, kind=kind, media_type="text/x-python", content=content
            ),
        ),
        manifest=ModelDelegationOutputManifest(
            entries=(
                ModelDelegationOutputManifestEntry(
                    path=_PATH,
                    kind=kind,
                    media_type="text/x-python",
                    sha256=digest,
                    size_bytes=len(data),
                    artifact_ref=f"sha256:{digest}",
                ),
            ),
            manifest_sha256=manifest_digest,
        ),
    )


def _refused(
    reason: EnumDelegationOutputFileRefusalReason,
    *,
    path: str | None = None,
    detail: str = "",
) -> ModelDelegationOutputExtractResult:
    return ModelDelegationOutputExtractResult(
        files=(),
        manifest=ModelDelegationOutputManifest(
            entries=(),
            refusals=(
                ModelDelegationOutputFileRefusal(
                    path=path, reason=reason, detail=detail
                ),
            ),
            manifest_sha256=hashlib.sha256(b"").hexdigest(),
        ),
    )


def test_fenced_files_without_a_language_tag_are_extracted() -> None:
    assert _run(f"```\n{_reply()}\n```") == _accepted()


def test_multiple_fences_select_the_last_conforming_files_object() -> None:
    reply = (
        f"```json\n{_reply('old')}\n```\n"
        f"```\n{_reply()}\n```\n"
        '```json\n{"files": [{"path": "src/answer.py"}]}\n```'
    )
    assert _run(reply) == _accepted()


def test_unterminated_fence_with_complete_json_is_extracted() -> None:
    assert _run(f"```json\n{_reply()}") == _accepted()


@pytest.mark.parametrize(
    "reply",
    [
        pytest.param("", id="empty"),
        pytest.param(" \n\t", id="whitespace"),
        pytest.param("The requested code prints the answer.", id="prose-only"),
        pytest.param(f"```\n{_CODE}```", id="untagged-code-without-files"),
        pytest.param('```json\n{"files": [', id="unterminated-json"),
        pytest.param('{"files": "code"}', id="wrong-files-type"),
        pytest.param('{"files": [{"path": "src/answer.py"}]}', id="missing-content"),
    ],
)
def test_unlocatable_reply_is_an_exact_whole_reply_refusal(reply: str) -> None:
    assert _run(reply) == _refused(
        EnumDelegationOutputFileRefusalReason.NO_FILES_OBJECT,
        detail="no JSON object with a files array: no_schema_conforming_json",
    )


def test_empty_files_array_names_the_missing_declared_file() -> None:
    assert _run('{"files": []}') == _refused(
        EnumDelegationOutputFileRefusalReason.MISSING, path=_PATH
    )


@pytest.mark.parametrize("kind", tuple(EnumDelegationOutputKind))
def test_every_declared_output_kind_is_preserved_in_file_and_manifest(
    kind: EnumDelegationOutputKind,
) -> None:
    assert _run(_reply(), kind=kind) == _accepted(kind=kind)


@pytest.mark.parametrize("content", [pytest.param("", id="empty"), "é"])
def test_empty_content_and_exact_utf8_byte_caps_are_accepted(content: str) -> None:
    cap = max(1, len(content.encode("utf-8")))
    assert _run(_reply(content), max_bytes=cap, max_total_bytes=cap) == _accepted(
        content
    )


def test_per_file_cap_counts_utf8_bytes() -> None:
    assert _run(_reply("é"), max_bytes=1) == _refused(
        EnumDelegationOutputFileRefusalReason.TOO_LARGE,
        path=_PATH,
        detail="2 bytes > 1",
    )


def test_total_cap_counts_utf8_bytes() -> None:
    assert _run(_reply("é"), max_total_bytes=1) == _refused(
        EnumDelegationOutputFileRefusalReason.TOTAL_TOO_LARGE,
        path=_PATH,
        detail="running total 2 > 1",
    )


@pytest.mark.parametrize(
    ("shape", "markers"),
    [
        (EnumDelegationOutputShape.JSON, ()),
        (EnumDelegationOutputShape.MARKDOWN, ("### ANSWER", "=== ANSWER ===")),
        (EnumDelegationOutputShape.PLAIN_TEXT, ("=== ANSWER ===", "FINAL:")),
    ],
)
def test_shared_extractor_maps_each_declared_output_shape(
    shape: EnumDelegationOutputShape, markers: tuple[str, ...]
) -> None:
    # The file handler uses a fixed JSON contract; shape mapping belongs to
    # the shared extractor that locates its files object.
    schema: dict[str, object] = {"type": "object"}
    contract = resolve_deliverable_contract(
        {"x-omninode-output-shape": shape.value, **schema}
    )
    assert contract == ModelDeliverableContract(
        output_shape=shape,
        min_deliverable_share=0.5,
        json_schema=schema if shape is EnumDelegationOutputShape.JSON else None,
        markers=markers,
        render_start_marker=markers[0] if markers else None,
    )
