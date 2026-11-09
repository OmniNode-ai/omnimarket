# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Read the T18b masked request and response corpus for a group's method-and-path identities.

The corpus is private and never enters this repository: its directory arrives in
ONEX_T18B_CORPUS_DIR. The manifest (manifest.json or manifest.yaml) names the masking
rules and, per pair, the identity, the request and response files, the cleanup name and
the sha256 of each file. This reader is coded against those field names; it refuses a
manifest, pair or masking rule it does not understand rather than ignoring it.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
import yaml

CORPUS_ENV = "ONEX_T18B_CORPUS_DIR"
MASK = "<masked>"
SKIP_REASON = (
    "AC3 (fixture replay) is pending on T18b, OMN-22158: the masked corpus "
    "location is {where} and holds no pair for {identities}"
)


@dataclass(frozen=True)
class ReplayPair:
    """One recorded request and response for one method-and-path identity."""

    identity: str
    request: dict[str, Any]
    response: dict[str, Any]
    cleanup: str


def _identity_of(raw: Any) -> str:
    if isinstance(raw, dict):
        return f"{str(raw['method']).upper()} {raw['path']}"
    method, _, path = str(raw).partition(" ")
    return f"{method.upper()} {path.strip()}"


def _load_manifest(corpus: Path) -> dict[str, Any]:
    for name in ("manifest.json", "manifest.yaml"):
        path = corpus / name
        if path.is_file():
            manifest = yaml.safe_load(path.read_text(encoding="utf-8"))
            if not isinstance(manifest, dict):
                raise AssertionError(f"{name} must be a mapping")
            return manifest
    raise AssertionError(f"{corpus} holds no manifest.json or manifest.yaml")


def _read_verified(corpus: Path, file_name: str, digest: str | None) -> dict[str, Any]:
    raw = (corpus / file_name).read_bytes()
    if digest is not None and hashlib.sha256(raw).hexdigest() != digest:
        raise AssertionError(f"{file_name} does not match its manifest sha256")
    document = json.loads(raw)
    if not isinstance(document, dict):
        raise AssertionError(f"{file_name} must be a JSON object")
    return document


def _digests(entry: dict[str, Any]) -> tuple[str | None, str | None]:
    digest = entry.get("sha256")
    if isinstance(digest, dict):
        return digest.get("request"), digest.get("response")
    return None, digest


def corpus_dir() -> Path | None:
    value = os.environ.get(CORPUS_ENV, "").strip()
    return Path(value) if value else None


def corpus_present() -> bool:
    """True when the corpus location names a directory; tests are defined only then."""
    corpus = corpus_dir()
    return corpus is not None and corpus.is_dir()


def load_pairs(
    identities: frozenset[str],
) -> tuple[list[ReplayPair], list[dict[str, Any]]]:
    """The group's pairs and the corpus's masking rules; skip only when none is present."""
    corpus = corpus_dir()
    selected: list[ReplayPair] = []
    rules: list[dict[str, Any]] = []
    if corpus is not None and corpus.is_dir():
        manifest = _load_manifest(corpus)
        rules = list(manifest.get("masking_rules", []))
        for entry in manifest.get("pairs", []):
            identity = _identity_of(entry["identity"])
            if identity not in identities:
                continue
            request_digest, response_digest = _digests(entry)
            selected.append(
                ReplayPair(
                    identity=identity,
                    request=_read_verified(corpus, entry["request"], request_digest),
                    response=_read_verified(corpus, entry["response"], response_digest),
                    cleanup=str(entry.get("cleanup", "")),
                )
            )
    if not selected:
        pytest.skip(
            SKIP_REASON.format(
                where="unset" if corpus is None else "set",
                identities=", ".join(sorted(identities)),
            )
        )
    return selected, rules


def _mask(value: Any, mask_path: list[str]) -> Any:
    """Replace the value at mask_path ('*' matches every key or list element)."""
    if not mask_path:
        return MASK
    head, rest = mask_path[0], mask_path[1:]
    if isinstance(value, list):
        return [_mask(item, rest) for item in value] if head == "*" else value
    if isinstance(value, dict):
        out = dict(value)
        for key in list(out):
            if head in ("*", key):
                out[key] = _mask(out[key], rest)
        return out
    return value


def apply_masking(
    document: dict[str, Any], rules: list[dict[str, Any]], section: str
) -> dict[str, Any]:
    """Apply the corpus's named masking rules whose path starts at section."""
    masked: dict[str, Any] = json.loads(json.dumps(document))
    for rule in rules:
        if "name" not in rule or "path" not in rule:
            raise AssertionError(f"masking rule {rule!r} lacks a name or a path")
        parts = str(rule["path"]).split(".")
        if parts[0] == section:
            masked = _mask(masked, parts[1:])
    return masked
