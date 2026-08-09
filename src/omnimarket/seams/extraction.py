# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Manifest-driven seam-graph extraction (OMN-15763).

Lives outside ``src/omnimarket/nodes/`` deliberately, mirroring
``omnibase_core.contract_graph`` (the library ``node_contract_graph_ir_compute``
delegates to for its own manifest-driven file discovery): the ONEX
node-purity gate scans node package directories for forbidden filesystem
calls, and the sanctioned pattern for a "pure" manifest-driven node is to
push the actual reads into a library module the node calls, not to avoid
reading files at all. Purity here rests on the methodology §2.2 argument —
the input is a pinned, already-materialized tree; checkout/fetch/ref
resolution happen upstream — not on the absence of ``Path.read_text()``.

Two extraction classes, kept separate:

* **Contract-declared** — every ``contract.yaml`` under the discovery roots
  is parsed for a ``seams:`` block (proposal step 1: declare seams where
  they are produced). This is the authoritative, typed source.
* **Code-level** — every ``*.py`` file under the discovery roots is scanned
  for Kafka producer/consumer topic literals, ``os.environ`` reads, and
  ``@ref`` pins. These are raw evidence, not declarations.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

import yaml

from omnimarket.seams.models.model_seam_graph import (
    EnumSeamGraphObservationKind,
    ModelSeamGraphCodeObservation,
    ModelSeamGraphEdgeDeclaration,
    ModelSeamGraphSourceHashEntry,
    ModelSeamGraphV1,
)

__all__ = ["extract_seam_graph"]

_EXCLUDED_PATH_PARTS = frozenset({".venv", "__pycache__", "node_modules", ".git"})

_PRODUCER_SEND_RE = re.compile(r"\bproducer\.send\(\s*[\"']([^\"']+)[\"']")
_CONSUMER_SUBSCRIBE_RE = re.compile(
    r"\bconsumer\.subscribe\(\s*\[?\s*[\"']([^\"']+)[\"']"
)
_ENV_READ_RE = re.compile(r"os\.environ(?:\.get)?\(?\[?\s*[\"']([A-Z][A-Z0-9_]*)[\"']")
_REF_PIN_RE = re.compile(r"@ref:\s*(\S+)")


def _repo_relative(path: Path, repo_base: Path) -> str:
    try:
        return str(path.relative_to(repo_base))
    except ValueError:
        return str(path)


def _is_excluded(path: Path, repo_base: Path) -> bool:
    """Exclude noise directories *within* the scanned tree only — never
    checks ancestor path segments above repo_base, since a pinned checkout
    may legitimately live under a directory a substring-match would
    otherwise wrongly exclude (e.g. a worktree root)."""

    relative_parts = Path(_repo_relative(path, repo_base)).parts
    return any(part in _EXCLUDED_PATH_PARTS for part in relative_parts)


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _discover_files(
    repo_base: Path, discovery_roots: tuple[str, ...], pattern: str
) -> list[Path]:
    found: set[Path] = set()
    for root in discovery_roots:
        root_path = repo_base / root
        if not root_path.exists():
            continue
        for candidate in root_path.rglob(pattern):
            if candidate.is_file() and not _is_excluded(candidate, repo_base):
                found.add(candidate)
    return sorted(found)


def _extract_declared_edges(
    contract_path: Path, repo_base: Path
) -> tuple[ModelSeamGraphEdgeDeclaration, ...]:
    try:
        raw = yaml.safe_load(contract_path.read_text(encoding="utf-8"))
    except (yaml.YAMLError, OSError):
        return ()
    if not isinstance(raw, dict):
        return ()
    seams = raw.get("seams")
    if not isinstance(seams, list):
        return ()

    source_contract_path = _repo_relative(contract_path, repo_base)
    edges: list[ModelSeamGraphEdgeDeclaration] = []
    for entry in seams:
        if not isinstance(entry, dict):
            continue
        try:
            edges.append(
                ModelSeamGraphEdgeDeclaration(
                    edge_id=str(entry["id"]),
                    seam=str(entry["seam"]),
                    role=str(entry["role"]),
                    source_contract_path=source_contract_path,
                    topic=str(entry["topic"]),
                    envelope_model=str(entry["envelope_model"]),
                    envelope_version=str(entry["envelope_version"]),
                )
            )
        except KeyError:
            # A seams: entry missing a required key is a malformed
            # declaration, not a code-level observation — skip it rather
            # than fabricate a partial edge. The contract-sweep-style gates
            # own flagging malformed contracts; this stays a pure reader.
            continue
    return tuple(edges)


def _extract_code_observations(
    source_path: Path, repo_base: Path
) -> tuple[ModelSeamGraphCodeObservation, ...]:
    try:
        text = source_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return ()

    repo_relative = _repo_relative(source_path, repo_base)
    observations: list[ModelSeamGraphCodeObservation] = []

    patterns: tuple[tuple[EnumSeamGraphObservationKind, re.Pattern[str]], ...] = (
        (EnumSeamGraphObservationKind.PRODUCER_SEND, _PRODUCER_SEND_RE),
        (EnumSeamGraphObservationKind.CONSUMER_SUBSCRIBE, _CONSUMER_SUBSCRIBE_RE),
        (EnumSeamGraphObservationKind.ENV_READ, _ENV_READ_RE),
        (EnumSeamGraphObservationKind.REF_PIN, _REF_PIN_RE),
    )

    for line_number, line in enumerate(text.splitlines(), start=1):
        for kind, pattern in patterns:
            for match in pattern.finditer(line):
                observations.append(
                    ModelSeamGraphCodeObservation(
                        source_path=repo_relative,
                        kind=kind,
                        value=match.group(1),
                        line_number=line_number,
                    )
                )

    return tuple(observations)


def extract_seam_graph(
    repo_base_path: str, discovery_roots: tuple[str, ...]
) -> ModelSeamGraphV1:
    """Walk ``discovery_roots`` (resolved against ``repo_base_path``, a
    pinned already-materialized tree) and emit ``seam-graph/v1`` plus the
    per-source sha256 manifest. Deterministic: sorted discovery, sorted
    output, so two runs over the same tree are byte-identical (AC7)."""

    repo_base = Path(repo_base_path)

    contract_paths = _discover_files(repo_base, discovery_roots, "contract.yaml")
    python_paths = _discover_files(repo_base, discovery_roots, "*.py")

    edges: list[ModelSeamGraphEdgeDeclaration] = []
    for contract_path in contract_paths:
        edges.extend(_extract_declared_edges(contract_path, repo_base))

    code_observations: list[ModelSeamGraphCodeObservation] = []
    for python_path in python_paths:
        code_observations.extend(_extract_code_observations(python_path, repo_base))

    manifest_entries: list[ModelSeamGraphSourceHashEntry] = []
    for source_path in (*contract_paths, *python_paths):
        manifest_entries.append(
            ModelSeamGraphSourceHashEntry(
                source_path=_repo_relative(source_path, repo_base),
                source_sha256=_sha256_bytes(source_path.read_bytes()),
            )
        )

    return ModelSeamGraphV1(
        discovery_roots=discovery_roots,
        edges=tuple(sorted(edges, key=lambda e: e.edge_id)),
        code_observations=tuple(
            sorted(
                code_observations,
                key=lambda o: (o.source_path, o.line_number, o.kind.value, o.value),
            )
        ),
        source_manifest=tuple(
            sorted(manifest_entries, key=lambda entry: entry.source_path)
        ),
    )
