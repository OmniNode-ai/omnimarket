# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""COMPUTE handler: an accepted reply becomes declared files and a manifest (OMN-19600).

The reply is located with the same extractor the quality gate uses
(``extract_deliverable``: the last JSON value in the reply that conforms to a
schema, fences and a preamble tolerated). It is located against a PERMISSIVE
schema, any ``{"files": [{"path", "content"}]}`` object, rather than the strict
declared one, so that one bad entry costs that entry and is named, instead of
costing the whole reply silently. Acceptance of the reply as a whole is still
the quality gate's decision, taken earlier against the strict schema.

Each entry is then judged on its own, in this order, and the first rule it
breaks is its refusal: path syntax, declared, not a duplicate, per-file size,
no secret, running total. A declared path the reply never supplied is a
MISSING refusal. Refusal details never quote file content, so a detected
secret is not copied into the manifest.
"""

from __future__ import annotations

import json

from omnibase_core.artifacts.secret_detector import SecretDetector
from omnibase_core.models.artifacts.model_artifact_ref import ModelArtifactRef
from omnibase_core.models.delegation.wire import EnumDelegationOutputShape

from omnimarket.delegation.deliverable_extraction import (
    ModelDeliverableContract,
    extract_deliverable,
)
from omnimarket.delegation.output_path_safety import check_relative_output_path
from omnimarket.models.delegation.wire.model_delegation_output_files import (
    EnumDelegationOutputFileRefusalReason,
    ModelDeclaredOutputs,
    ModelDelegationOutputFile,
    ModelDelegationOutputFileRefusal,
    ModelDelegationOutputManifest,
    ModelDelegationOutputManifestEntry,
    compute_manifest_sha256,
)
from omnimarket.nodes.node_delegation_output_extract_compute.models.model_delegation_output_extract import (
    ModelDelegationOutputExtractRequest,
    ModelDelegationOutputExtractResult,
)

#: Locates any files object; the strict declared schema is the gate's job.
_PERMISSIVE_FILES_SCHEMA: dict[str, object] = {
    "type": "object",
    "required": ["files"],
    "properties": {
        "files": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["path", "content"],
                "properties": {
                    "path": {"type": "string"},
                    "content": {"type": "string"},
                },
            },
        }
    },
}

#: The share floor is an acceptance rule, already applied by the gate.
_LOCATE_CONTRACT = ModelDeliverableContract(
    output_shape=EnumDelegationOutputShape.JSON,
    min_deliverable_share=1e-9,
    json_schema=_PERMISSIVE_FILES_SCHEMA,
)

_R = EnumDelegationOutputFileRefusalReason


def _refusal(
    path: str | None, reason: EnumDelegationOutputFileRefusalReason, detail: str = ""
) -> ModelDelegationOutputFileRefusal:
    return ModelDelegationOutputFileRefusal(
        path=None if path is None else path[:1024], reason=reason, detail=detail[:512]
    )


def extract_declared_outputs(
    request: ModelDelegationOutputExtractRequest,
    *,
    secret_detector: SecretDetector | None = None,
) -> ModelDelegationOutputExtractResult:
    """Return the accepted files and their manifest; never raises on model output."""
    detector = secret_detector or SecretDetector()
    declared: ModelDeclaredOutputs = request.declared_outputs
    declared_by_path = declared.by_path()

    located = extract_deliverable(request.response_text, _LOCATE_CONTRACT)
    if located.refusal is not None:
        empty: tuple[ModelDelegationOutputManifestEntry, ...] = ()
        return ModelDelegationOutputExtractResult(
            files=(),
            manifest=ModelDelegationOutputManifest(
                entries=empty,
                refusals=(
                    _refusal(
                        None,
                        _R.NO_FILES_OBJECT,
                        f"no JSON object with a files array: {located.refusal.value}",
                    ),
                ),
                manifest_sha256=compute_manifest_sha256(empty),
            ),
        )

    items = json.loads(located.deliverable)["files"]
    accepted: dict[
        str, tuple[ModelDelegationOutputFile, ModelDelegationOutputManifestEntry]
    ] = {}
    refusals: list[ModelDelegationOutputFileRefusal] = []
    total = 0
    for item in items:
        path = item["path"]
        content = item["content"]
        try:
            check_relative_output_path(path)
        except ValueError as exc:
            refusals.append(_refusal(path, _R.PATH_UNSAFE, str(exc)))
            continue
        spec = declared_by_path.get(path)
        if spec is None:
            refusals.append(_refusal(path, _R.UNDECLARED_PATH))
            continue
        if path in accepted:
            refusals.append(_refusal(path, _R.DUPLICATE))
            continue
        data = content.encode("utf-8")
        if len(data) > spec.max_bytes:
            refusals.append(
                _refusal(path, _R.TOO_LARGE, f"{len(data)} bytes > {spec.max_bytes}")
            )
            continue
        if detector.contains_secret(data):
            refusals.append(
                _refusal(path, _R.SECRET_DETECTED, "secret pattern matched")
            )
            continue
        if total + len(data) > declared.max_total_bytes:
            refusals.append(
                _refusal(
                    path,
                    _R.TOTAL_TOO_LARGE,
                    f"running total {total + len(data)} > {declared.max_total_bytes}",
                )
            )
            continue
        total += len(data)
        ref = ModelArtifactRef.from_bytes(data)
        accepted[path] = (
            ModelDelegationOutputFile(
                path=path, kind=spec.kind, media_type=spec.media_type, content=content
            ),
            ModelDelegationOutputManifestEntry(
                path=path,
                kind=spec.kind,
                media_type=spec.media_type,
                sha256=ref.hex_digest,
                size_bytes=len(data),
                artifact_ref=ref.ref,
            ),
        )

    supplied = {item["path"] for item in items}
    refusals.extend(
        _refusal(spec.path, _R.MISSING)
        for spec in declared.files
        if spec.path not in supplied
    )
    ordered = [accepted[spec.path] for spec in declared.files if spec.path in accepted]
    entries = tuple(entry for _, entry in ordered)
    return ModelDelegationOutputExtractResult(
        files=tuple(file for file, _ in ordered),
        manifest=ModelDelegationOutputManifest(
            entries=entries,
            refusals=tuple(refusals),
            manifest_sha256=compute_manifest_sha256(entries),
        ),
    )


class HandlerDelegationOutputExtract:
    """COMPUTE handler: accepted reply and declaration in, files and manifest out."""

    def handle(
        self, request: ModelDelegationOutputExtractRequest
    ) -> ModelDelegationOutputExtractResult:
        return extract_declared_outputs(request)


__all__ = ["HandlerDelegationOutputExtract", "extract_declared_outputs"]
