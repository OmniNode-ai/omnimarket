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
* **Code-level** — every ``*.py`` file under the discovery roots is parsed
  with ``ast`` for Kafka producer/consumer topic literals and ``os.environ``
  reads (comments and docstrings never produce a false observation, and
  multiline calls resolve correctly because ``ast`` — not a per-line regex —
  sees the whole call). ``@ref`` pins are scanned separately, restricted to
  actual comment tokens via ``tokenize`` (never a string literal that merely
  contains the substring ``@ref:``). These are raw evidence, not
  declarations.
"""

from __future__ import annotations

import ast
import hashlib
import io
import re
import tokenize
from pathlib import Path

import yaml
from pydantic import ValidationError

from omnimarket.seams.models.model_seam_graph import (
    EnumSeamGraphObservationKind,
    ModelSeamGraphCodeObservation,
    ModelSeamGraphEdgeDeclaration,
    ModelSeamGraphSourceHashEntry,
    ModelSeamGraphV1,
)

__all__ = ["extract_seam_graph"]

_EXCLUDED_PATH_PARTS = frozenset({".venv", "__pycache__", "node_modules", ".git"})

_ENV_VAR_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")
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


def _resolve_confined_root(repo_base: Path, root: str) -> Path | None:
    """Resolve one discovery root against ``repo_base``, confined inside it.

    Returns ``None`` (never raises) for an invalid root so the caller can
    skip it and keep scanning the remaining valid roots:

    * an **absolute** ``root`` is rejected outright — pathlib silently
      discards the left operand when the right side of ``/`` is itself
      absolute (``Path("/base") / "/etc" == Path("/etc")``), so an absolute
      root would otherwise escape ``repo_base`` completely;
    * a ``..`` **traversal** or a **symlink** that resolves outside
      ``repo_base`` is rejected by requiring the resolved candidate to sit
      under the resolved base (``candidate.relative_to(resolved_base)``).
    """

    if Path(root).is_absolute():
        return None
    resolved_base = repo_base.resolve()
    candidate = (repo_base / root).resolve()
    try:
        candidate.relative_to(resolved_base)
    except ValueError:
        return None
    return candidate


def _discover_files(
    repo_base: Path, discovery_roots: tuple[str, ...], pattern: str
) -> list[Path]:
    found: set[Path] = set()
    for root in discovery_roots:
        root_path = _resolve_confined_root(repo_base, root)
        if root_path is None or not root_path.exists():
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
            # No str(...) coercion: a wrong-typed field (e.g. a null or
            # numeric `seam`) must fail pydantic validation and be skipped,
            # not be silently stringified into a plausible-looking but wrong
            # value ("None", "123") that then poisons the emitted graph.
            edges.append(
                ModelSeamGraphEdgeDeclaration(
                    edge_id=entry["id"],
                    seam=entry["seam"],
                    role=entry["role"],
                    source_contract_path=source_contract_path,
                    topic=entry["topic"],
                    envelope_model=entry["envelope_model"],
                    envelope_version=entry["envelope_version"],
                )
            )
        except (KeyError, ValidationError):
            # A seams: entry missing a required key, or carrying a
            # wrong-typed value, is a malformed declaration, not a
            # code-level observation — skip it rather than fabricate or
            # coerce a partial edge. The contract-sweep-style gates own
            # flagging malformed contracts; this stays a pure reader.
            continue
    return tuple(edges)


def _string_constant(node: ast.expr | None) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _attr_base_name(value: ast.expr) -> str:
    """Best-effort trailing identifier for an attribute chain — e.g.
    ``self.producer`` and bare ``producer`` both yield ``"producer"`` — so
    the classifier matches both the fixture's bare-name idiom and a real
    call site's ``self.producer.send(...)`` shape."""

    if isinstance(value, ast.Name):
        return value.id
    if isinstance(value, ast.Attribute):
        return value.attr
    return ""


def _call_topic_literal(call: ast.Call) -> str | None:
    """First string literal argument, unwrapping a single-element list
    (the ``consumer.subscribe(["topic"])`` idiom)."""

    if not call.args:
        return None
    first = call.args[0]
    literal = _string_constant(first)
    if literal is not None:
        return literal
    if isinstance(first, ast.List) and first.elts:
        return _string_constant(first.elts[0])
    return None


def _classify_call(call: ast.Call) -> EnumSeamGraphObservationKind | None:
    func = call.func
    if not isinstance(func, ast.Attribute):
        return None
    if func.attr == "send" and _attr_base_name(func.value) == "producer":
        return EnumSeamGraphObservationKind.PRODUCER_SEND
    if func.attr == "subscribe" and _attr_base_name(func.value) == "consumer":
        return EnumSeamGraphObservationKind.CONSUMER_SUBSCRIBE
    if (
        func.attr == "get"
        and isinstance(func.value, ast.Attribute)
        and func.value.attr == "environ"
        and _attr_base_name(func.value.value) == "os"
    ):
        return EnumSeamGraphObservationKind.ENV_READ
    return None


def _is_os_environ_subscript(node: ast.Subscript) -> bool:
    value = node.value
    return (
        isinstance(value, ast.Attribute)
        and value.attr == "environ"
        and _attr_base_name(value.value) == "os"
    )


def _extract_ast_observations(
    tree: ast.AST, repo_relative: str
) -> list[ModelSeamGraphCodeObservation]:
    observations: list[ModelSeamGraphCodeObservation] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            kind = _classify_call(node)
            if kind is None:
                continue
            if kind is EnumSeamGraphObservationKind.ENV_READ:
                value = _string_constant(node.args[0]) if node.args else None
            else:
                value = _call_topic_literal(node)
            if not value or not value.strip():
                continue
            if (
                kind is EnumSeamGraphObservationKind.ENV_READ
                and not _ENV_VAR_NAME_RE.match(value)
            ):
                continue
            observations.append(
                ModelSeamGraphCodeObservation(
                    source_path=repo_relative,
                    kind=kind,
                    value=value,
                    line_number=node.lineno,
                )
            )
        elif isinstance(node, ast.Subscript) and _is_os_environ_subscript(node):
            value = _string_constant(node.slice)
            if value is not None and _ENV_VAR_NAME_RE.match(value):
                observations.append(
                    ModelSeamGraphCodeObservation(
                        source_path=repo_relative,
                        kind=EnumSeamGraphObservationKind.ENV_READ,
                        value=value,
                        line_number=node.lineno,
                    )
                )
    return observations


def _extract_ref_pin_observations(
    text: str, repo_relative: str
) -> list[ModelSeamGraphCodeObservation]:
    """``@ref:`` pins, restricted to actual comment TOKENS (``tokenize``),
    never a string literal or docstring that merely contains the substring
    ``@ref:``."""

    observations: list[ModelSeamGraphCodeObservation] = []
    try:
        for tok in tokenize.generate_tokens(io.StringIO(text).readline):
            if tok.type != tokenize.COMMENT:
                continue
            for match in _REF_PIN_RE.finditer(tok.string):
                observations.append(
                    ModelSeamGraphCodeObservation(
                        source_path=repo_relative,
                        kind=EnumSeamGraphObservationKind.REF_PIN,
                        value=match.group(1),
                        line_number=tok.start[0],
                    )
                )
    except (tokenize.TokenError, IndentationError, SyntaxError):
        return observations
    return observations


def _extract_code_observations(
    source_path: Path, repo_base: Path
) -> tuple[ModelSeamGraphCodeObservation, ...]:
    try:
        text = source_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return ()

    repo_relative = _repo_relative(source_path, repo_base)
    observations: list[ModelSeamGraphCodeObservation] = []

    try:
        tree = ast.parse(text)
    except SyntaxError:
        tree = None

    if tree is not None:
        observations.extend(_extract_ast_observations(tree, repo_relative))

    observations.extend(_extract_ref_pin_observations(text, repo_relative))

    return tuple(observations)


def extract_seam_graph(
    repo_base_path: str, discovery_roots: tuple[str, ...]
) -> ModelSeamGraphV1:
    """Walk ``discovery_roots`` (resolved against ``repo_base_path``, a
    pinned already-materialized tree) and emit ``seam-graph/v1`` plus the
    per-source sha256 manifest. Deterministic: sorted discovery, sorted
    output, so two runs over the same tree are byte-identical (AC7)."""

    repo_base = Path(repo_base_path).resolve()

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
