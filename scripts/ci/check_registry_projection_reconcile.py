#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""CI gate: a REGISTRY-shaped projection may not depend on topic retention.

OMN-17446. ``tenant_registry_mirror`` is the relation both the OMN-16804
write-path resolver and migration 0034's ``USING`` JOIN resolve tenant identity
against. Its only source was ``onex.tenant.events`` at
``log.retention.hours=168``. Seven days. Four of the five tenants that had ever
written a delegation event therefore had their ``TENANT_CREATED`` aged off the
topic before the projection was ever deployed, and no consumer restart, offset
reset or redeploy could materialize them -- the events were gone. The
migration's own pre-guard ("the mirror holds a row for every distinct
``tenant_id`` in ``delegation_events``") became a condition that could never be
satisfied. It was correctly fail-closed, so it would have refused forever.

THE CLASS, STATED PRECISELY. Every component was individually correct. The
projection wrote what it consumed; the resolver refused what it could not
resolve; the migration refused rather than mis-converting. The defect lives in
the join between a relation's ROLE and its SOURCE's retention policy:

* a REGISTRY is a relation another identity-resolving path depends on -- a
  write path outside the owning node, or a migration's conversion clause. Those
  callers need it to be TOTAL over the corpus, because a miss is a refusal.
* a TIME-RETAINED topic is inherently PARTIAL. It holds a window, not a
  corpus. A projection built on one is a partial materialization by
  construction, which is exactly what a projection should be.

Wiring the second under the first produces a relation that is required to be
total and structurally cannot be. Nothing observes that at build time: no unit
test, no integration test, and no live probe of a healthy lane. It only becomes
visible once a row has aged out, which is months after the contract shipped.

WHY A STATIC GATE (omni_home CLAUDE.md rule 5). Detection that is not a
pre-merge gate gets ignored, and this particular defect has no runtime signal
until it is already unrecoverable. So the check is static, over the contract
corpus and the migration corpus, and it runs in pre-commit AND in CI.

WHAT IS CHECKED. For every relation a node contract declares with ``access:
write`` or ``read_write``, where that relation is ALSO read by

  (a) a DIFFERENT node's migration, in an identity-CONVERSION clause
      (``ALTER COLUMN ... TYPE ... USING``, which resolves one identifier into
      another and refuses the whole migration on a miss); or
  (b) a shared write-path module under ``src/omnimarket/projection/`` that
      SQL-reads it and is imported by a node other than the relation's owner --

the owning contract must satisfy at least one of:

  1. it subscribes to a COMPACTION-class topic (a keyed changelog retains the
     latest value per key indefinitely, so the relation can always be rebuilt);
     or
  2. it carries a complete entry in
     ``scripts/ci/registry_projection_reconcile_allowlist.yaml`` naming a real,
     citable reconcile path.

These are options A and B of the ticket. Both are legitimate; the operator
declined A for ``onex.tenant.events`` specifically and authorized B, so the one
registry that exists today is cleared by an allowlist entry citing the re-emit
primitive that actually landed.

FAIL-CLOSED. A topic whose cleanup class cannot be established is treated as
time-retained. That is not pedantry: ``onex.tenant.events``, the topic that
caused this, is declared in no ``topics.yaml`` in this repo at all -- it is
owned by onex-api in another repository. A gate that passed on "I could not
determine this" would have passed on the original defect.

ESCAPE HATCH. The allowlist, and nothing else. Every field is required and no
field may be blank, because a half-filled exemption reads as reviewed to
somebody skimming YAML. An exemption whose relation no longer produces a
finding is itself a finding -- that is what stops the list accreting dead lines
that eventually cover a relation whose reconcile path was deleted.

FIXING A VIOLATION. Give the registry a reconcile path, or move it onto a
compacted topic. Adding an allowlist entry with no real reconcile path behind
it is not a fix; it is the defect with a comment on it.

Exit codes: 0 = clean; 1 = a registry-shaped projection has no reconcile path;
2 = invocation error (run from repo root, or the gate's own inputs are
unreadable).
"""

from __future__ import annotations

import ast
import pathlib
import re
import sys
from dataclasses import dataclass

import yaml

_ALLOWLIST_RELPATH = pathlib.Path(
    "scripts/ci/registry_projection_reconcile_allowlist.yaml"
)

# Every field is load-bearing. `reconcile_path` is the claim being made,
# `ticket` is where it was ruled, and `reason` is what a reviewer reads.
_REQUIRED_EXEMPTION_FIELDS = ("relation", "node", "reconcile_path", "ticket", "reason")

_WRITE_ACCESS = frozenset({"write", "read_write"})

# A relation name is a bare SQL identifier; anchor on word boundaries so
# `thing_registry_mirror` does not match `thing_registry_mirror_archive`.
_SQL_READ_RE = re.compile(
    r"\b(?:FROM|JOIN)\s+(?:ONLY\s+)?(?:[a-z_][a-z0-9_]*\.)?([a-z_][a-z0-9_]+)\b",
    re.IGNORECASE,
)

# An identity CONVERSION, not merely a read: the column is rewritten through
# the joined relation, so one unresolved row refuses the whole migration.
_IDENTITY_CONVERSION_RE = re.compile(
    r"\bALTER\s+COLUMN\b[^;]*?\bTYPE\b[^;]*?\bUSING\b",
    re.IGNORECASE | re.DOTALL,
)


class GateInputError(RuntimeError):
    """The gate could not read one of its own inputs.

    Raised rather than swallowed. A gate that cannot read its own exemption
    list has not passed -- it has not run, and the two must not look alike
    (the same posture omni_home CLAUDE.md rule 23 takes for the public-repo
    hygiene vocabulary).
    """


@dataclass(frozen=True)
class Violation:
    relation: str
    node: str
    subscribe_topics: tuple[str, ...]
    readers: tuple[str, ...]
    detail: str

    def render(self) -> str:
        topics = ", ".join(self.subscribe_topics) or "(none)"
        readers = ", ".join(self.readers) or "(none)"
        return (
            f"{self.relation} (written by {self.node}, subscribed topics: "
            f"{topics}; resolved against by: {readers}) — {self.detail}"
        )


def _compaction_class_marker() -> str:
    """The topic-type segment whose cleanup policy is compaction.

    Read from the ONEX topic taxonomy rather than hardcoded here, so this gate
    cannot drift from the policy it claims to enforce. ``CLEANUP_POLICY_*`` in
    ``omnibase_core.constants.constants_topic_taxonomy`` assigns
    ``compact,delete`` to snapshots and plain ``delete`` to commands, events
    and intents.
    """
    try:
        from omnibase_core.constants.constants_topic_taxonomy import (
            CLEANUP_POLICY_SNAPSHOTS,
            TOPIC_TYPE_SNAPSHOTS,
        )
    except ImportError as exc:  # pragma: no cover - dependency is declared
        raise GateInputError(
            "cannot read the ONEX topic taxonomy from omnibase_core, so no "
            "topic's cleanup policy can be established. Refusing to report a "
            "pass on inputs the gate could not read."
        ) from exc
    if "compact" not in CLEANUP_POLICY_SNAPSHOTS:
        raise GateInputError(
            "the topic taxonomy no longer assigns a compaction policy to "
            f"{TOPIC_TYPE_SNAPSHOTS!r} ({CLEANUP_POLICY_SNAPSHOTS!r}); this "
            "gate's compaction rule must be re-derived before it can pass."
        )
    return str(TOPIC_TYPE_SNAPSHOTS)


def _is_compaction_class(topic: str, marker: str) -> bool:
    """True when the topic is provably a keyed changelog.

    Matches both live naming families: the taxonomy's ``onex.<domain>.snapshots``
    and the ``onex.snapshot.<...>`` form omnimarket's projection exposures use
    (e.g. ``onex.snapshot.projection.delegation.decisions.v1``). Anything else
    -- including a topic this repo does not declare at all -- is time-retained
    as far as this gate is concerned.
    """
    segments = topic.split(".")
    singular = marker.rstrip("s")
    return any(segment in {marker, singular} for segment in segments[1:])


def _load_exemptions(repo_root: pathlib.Path) -> tuple[dict[str, object], ...]:
    path = repo_root / _ALLOWLIST_RELPATH
    if not path.is_file():
        raise GateInputError(f"exemption list not found: {path}")
    try:
        loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise GateInputError(f"exemption list is not parseable YAML: {path}") from exc
    if loaded is None:
        return ()
    if not isinstance(loaded, dict) or "exemptions" not in loaded:
        raise GateInputError(
            f"exemption list must be a mapping with an 'exemptions' key: {path}"
        )
    entries = loaded["exemptions"] or []
    if not isinstance(entries, list):
        raise GateInputError(f"'exemptions' must be a list: {path}")
    for entry in entries:
        if not isinstance(entry, dict):
            raise GateInputError(f"each exemption must be a mapping: {path}")
    return tuple(entries)


def _missing_fields(entry: dict[str, object]) -> tuple[str, ...]:
    """Required fields that are absent, or present but blank.

    A blank ``reason:`` reads as filled-in to a reviewer skimming the file, so
    it is treated as absent rather than as a weaker form of present.
    """
    missing: list[str] = []
    for field in _REQUIRED_EXEMPTION_FIELDS:
        value = entry.get(field)
        if not isinstance(value, str) or not value.strip():
            missing.append(field)
    return tuple(missing)


def _contracts(repo_root: pathlib.Path) -> list[tuple[str, dict[str, object]]]:
    nodes_dir = repo_root / "src" / "omnimarket" / "nodes"
    found: list[tuple[str, dict[str, object]]] = []
    for contract_path in sorted(nodes_dir.glob("*/contract.yaml")):
        try:
            loaded = yaml.safe_load(contract_path.read_text(encoding="utf-8"))
        except yaml.YAMLError as exc:
            raise GateInputError(
                f"contract is not parseable YAML: {contract_path}"
            ) from exc
        if isinstance(loaded, dict):
            found.append((contract_path.parent.name, loaded))
    return found


def _written_relations(
    contracts: list[tuple[str, dict[str, object]]],
) -> dict[str, tuple[str, tuple[str, ...]]]:
    """relation -> (owning node dir, that contract's subscribed topics)."""
    owned: dict[str, tuple[str, tuple[str, ...]]] = {}
    for node, contract in contracts:
        db_io = contract.get("db_io") or {}
        tables = db_io.get("db_tables") or [] if isinstance(db_io, dict) else []
        event_bus = contract.get("event_bus") or {}
        raw_topics = (
            event_bus.get("subscribe_topics") or []
            if isinstance(event_bus, dict)
            else []
        )
        topics = tuple(str(topic) for topic in raw_topics)
        for table in tables:
            if not isinstance(table, dict):
                continue
            if str(table.get("access", "")).strip() not in _WRITE_ACCESS:
                continue
            name = str(table.get("name", "")).strip()
            if name:
                # A relation with two writers is still one registry; the first
                # writer in sorted node order names it, and the finding is
                # about the relation, not about which node got there first.
                owned.setdefault(name, (node, topics))
    return owned


def _migration_readers(
    repo_root: pathlib.Path, relations: set[str], owner_of: dict[str, str]
) -> dict[str, set[str]]:
    """Relations resolved against by a DIFFERENT node's identity conversion.

    Two narrowings, both deliberate, because the wide form is what makes a
    gate that nobody can satisfy:

    * A node's own migration creating, indexing or seeding its own relation is
      not another path depending on it.
    * A migration that merely SELECTs the relation into a VIEW is not
      resolving identity against it. A view over a partial relation is
      partial, which is ordinary and fine. What is NOT fine is an
      ``ALTER COLUMN ... TYPE ... USING`` conversion: that rewrites a column
      through the relation, so a single unresolved row refuses the whole
      migration, permanently, which is precisely the OMN-17446 dead end.

    Without the second narrowing this fires on six savings VIEW migrations
    over ``delegation_events`` and one capsule-effectiveness view -- none of
    which is a registry read.
    """
    readers: dict[str, set[str]] = {}
    nodes_dir = repo_root / "src" / "omnimarket" / "nodes"
    for sql_path in sorted(nodes_dir.glob("*/migrations/*.sql")):
        reading_node = sql_path.parents[1].name
        text = sql_path.read_text(encoding="utf-8", errors="replace")
        if not _IDENTITY_CONVERSION_RE.search(text):
            continue
        for match in _SQL_READ_RE.finditer(text):
            name = match.group(1).lower()
            if name not in relations or owner_of.get(name) == reading_node:
                continue
            readers.setdefault(name, set()).add(
                f"{reading_node}/migrations/{sql_path.name}"
            )
    return readers


def _module_importers(repo_root: pathlib.Path, module_stem: str) -> set[str]:
    """Node directories importing ``omnimarket.projection.<module_stem>``."""
    importers: set[str] = set()
    nodes_dir = repo_root / "src" / "omnimarket" / "nodes"
    needle = f"projection.{module_stem}"
    for py_path in nodes_dir.rglob("*.py"):
        if needle in py_path.read_text(encoding="utf-8", errors="replace"):
            importers.add(py_path.relative_to(nodes_dir).parts[0])
    return importers


def _sql_strings(tree: ast.AST) -> list[str]:
    """Every string this module BUILDS, with docstrings excluded.

    Docstrings are excluded because they document; they do not execute. The
    module docstring of ``projection/pr_ledger_projection.py`` contains a
    literal ``select count(*) from pr_lifecycle_ledger_entries`` as an operator
    DoD probe, which a text grep reads as that module querying the relation.
    It does not.

    F-strings are rendered by substituting module-level ``NAME = "literal"``
    bindings, because the resolver this gate exists for composes its SQL that
    way -- ``f"SELECT tenant_uuid FROM {TENANT_REGISTRY_MIRROR_TABLE}"`` -- so
    the relation name never appears next to ``FROM`` in the source at all.
    """
    docstrings = {
        id(node.body[0].value)
        for node in ast.walk(tree)
        if isinstance(
            node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
        )
        and node.body
        and isinstance(node.body[0], ast.Expr)
        and isinstance(node.body[0].value, ast.Constant)
        and isinstance(node.body[0].value.value, str)
    }

    bindings: dict[str, str] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        target = node.targets[0]
        value = node.value
        if (
            isinstance(target, ast.Name)
            and isinstance(value, ast.Constant)
            and isinstance(value.value, str)
        ):
            bindings[target.id] = value.value

    rendered: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if id(node) not in docstrings:
                rendered.append(node.value)
        elif isinstance(node, ast.JoinedStr):
            parts: list[str] = []
            for piece in node.values:
                if isinstance(piece, ast.Constant) and isinstance(piece.value, str):
                    parts.append(piece.value)
                elif isinstance(piece, ast.FormattedValue) and isinstance(
                    piece.value, ast.Name
                ):
                    parts.append(bindings.get(piece.value.id, ""))
            rendered.append("".join(parts))
    return rendered


def _write_path_readers(
    repo_root: pathlib.Path, relations: set[str], owner_of: dict[str, str]
) -> dict[str, set[str]]:
    """Relations SQL-read by a shared write-path module another node imports.

    "Another node" is the load-bearing half. ``projection/`` holds modules
    shared across nodes, so directory position cannot say who owns a read --
    a node's own helper living there is not a second path depending on the
    relation, while a resolver two OTHER nodes import to stamp THEIR rows is
    exactly the registry shape.
    """
    readers: dict[str, set[str]] = {}
    projection_dir = repo_root / "src" / "omnimarket" / "projection"
    if not projection_dir.is_dir():
        return readers
    for py_path in sorted(projection_dir.rglob("*.py")):
        try:
            tree = ast.parse(py_path.read_text(encoding="utf-8", errors="replace"))
        except SyntaxError as exc:
            raise GateInputError(f"cannot parse write-path module {py_path}") from exc
        read_names = {
            match.group(1).lower()
            for text in _sql_strings(tree)
            for match in _SQL_READ_RE.finditer(text)
        }
        hits = read_names & relations
        if not hits:
            continue
        importers = _module_importers(repo_root, py_path.stem)
        for name in hits:
            if importers - {owner_of.get(name, "")}:
                readers.setdefault(name, set()).add(
                    f"projection/{py_path.relative_to(projection_dir)}"
                )
    return readers


def scan(
    repo_root: pathlib.Path,
    *,
    exemptions: tuple[dict[str, object], ...] | None = None,
) -> list[Violation]:
    """Return every registry-shaped projection with no reconcile path.

    ``exemptions`` is injectable so a test can supply an empty list and prove
    the gate is probative against the real tree -- a clean result is worth
    nothing without a positive control (omni_home CLAUDE.md rule 16).
    """
    marker = _compaction_class_marker()
    if exemptions is None:
        exemptions = _load_exemptions(repo_root)

    contracts = _contracts(repo_root)
    owned = _written_relations(contracts)
    owner_of = {relation: node for relation, (node, _) in owned.items()}
    relations = set(owned)

    readers: dict[str, set[str]] = {}
    for source in (
        _migration_readers(repo_root, relations, owner_of),
        _write_path_readers(repo_root, relations, owner_of),
    ):
        for relation, sites in source.items():
            readers.setdefault(relation, set()).update(sites)

    exempt_by_relation: dict[str, dict[str, object]] = {}
    for entry in exemptions:
        key = entry.get("relation")
        if isinstance(key, str) and key.strip():
            exempt_by_relation[key.strip()] = entry
        else:
            exempt_by_relation.setdefault("", entry)

    violations: list[Violation] = []

    for relation in sorted(readers):
        node, topics = owned[relation]
        exemption = exempt_by_relation.get(relation)

        if any(_is_compaction_class(topic, marker) for topic in topics):
            continue

        if exemption is None:
            violations.append(
                Violation(
                    relation=relation,
                    node=node,
                    subscribe_topics=topics,
                    readers=tuple(sorted(readers[relation])),
                    detail=(
                        "this relation is a REGISTRY -- another node's "
                        "migration or a shared write path resolves identity "
                        "against it, so a miss is a refusal, not a gap -- but "
                        "its only source is a time-retained topic. Rows that "
                        "age off the topic can never be materialized. Give it "
                        "a reconcile path that republishes the corpus from the "
                        "authoritative source and declare that path in "
                        f"{_ALLOWLIST_RELPATH}, or move it onto a "
                        "compaction-class topic."
                    ),
                )
            )
            continue

        missing = _missing_fields(exemption)
        if missing:
            violations.append(
                Violation(
                    relation=relation,
                    node=node,
                    subscribe_topics=topics,
                    readers=tuple(sorted(readers[relation])),
                    detail=(
                        "its exemption is incomplete — missing or blank: "
                        f"{', '.join(missing)}. Every field is required: an "
                        "exemption a reviewer cannot evaluate is not an "
                        "exemption."
                    ),
                )
            )

    # An exemption that has outlived its finding. Left in place, the list
    # accretes dead lines nobody re-derives, until one silently covers a
    # relation whose reconcile path was deleted.
    for relation in sorted(exempt_by_relation):
        if relation in readers:
            continue
        entry = exempt_by_relation[relation]
        node = str(entry.get("node") or "(unnamed node)")
        violations.append(
            Violation(
                relation=relation or "(unnamed relation)",
                node=node,
                subscribe_topics=(),
                readers=(),
                detail=(
                    "stale exemption — this relation produces no finding, so "
                    "the entry grants nothing and no longer describes a real "
                    f"constraint. Delete it from {_ALLOWLIST_RELPATH}."
                ),
            )
        )

    return violations


def main() -> int:
    repo_root = pathlib.Path.cwd()
    if not (repo_root / "src" / "omnimarket" / "nodes").is_dir():
        print(
            "ERROR: run from repo root (src/omnimarket/nodes not found)",
            file=sys.stderr,
        )
        return 2

    try:
        violations = scan(repo_root)
    except GateInputError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    if violations:
        print(
            "Registry-projection reconcile gate FAILED (OMN-17446) — a "
            "relation other identity-resolving paths depend on is fed only by "
            "a time-retained topic, so rows that age off the topic can never "
            "be materialized and every dependent read refuses forever:",
            file=sys.stderr,
        )
        for violation in violations:
            print(f"  - {violation.render()}", file=sys.stderr)
        print(
            "\nGive the registry a reconcile path or a compacted source — "
            "never add an exemption without one behind it.",
            file=sys.stderr,
        )
        return 1

    print(
        "Registry-projection reconcile gate OK: every registry-shaped "
        "projection has a reconcile path or a compacted source."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
