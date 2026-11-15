# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Gathers raw seam-graph facts for every node a rule's repos declare.

Lives outside ``src/omnimarket/nodes/`` deliberately (like
``omnimarket.seams.extraction``): it walks the pinned, already-materialized
checkout trees named in the request, and the ONEX node-purity gate forbids
filesystem calls inside node packages. It carries no effect of its own: it
only reports the facts each step gathers, deterministically, for the defect
derivator to judge. A contract's declared topics are never re-implemented
here — they come from the one reader, ``HandlerContractTopicReader``.

Steps, one helper each:

1. **CONTRACTS** — every contract file each repo rule globs is parsed into
   a ``ModelContractDocument`` and handed to the topic reader; its uses are
   grouped into the per-node topic buckets.
2. **OVERLAY** — whether the node needs an overlay, which keys its example
   overlay declares, and whether its overlay row exists with which keys.
3. **SYNC IO** — ``def handle`` bodies whose source matches a sync-IO
   marker regex.
4. **LEDGER** — whole-file matches of a ledger-reader marker regex.
5. **REFERENCES** — one walking pass over each repo's caller scan dirs,
   naming the files outside a node's own directory that reference it.
6. **IMPORTS** — cross-repo imports and whether a seam test covers them.
"""

from __future__ import annotations

import ast
import os
import re
from pathlib import Path
from typing import NamedTuple

import yaml

from omnimarket.models.model_contract_topics import (
    EnumContractTopicShape,
    ModelContractDocument,
    ModelContractTopicReport,
    ModelContractTopicRequest,
    ModelContractTopicRule,
)
from omnimarket.models.model_seam_graph_defects import (
    ModelSeamGraphFacts,
    ModelSeamGraphRequest,
    ModelSeamGraphRule,
    ModelSeamImportFact,
    ModelSeamNodeFact,
)
from omnimarket.nodes.node_contract_topic_reader_compute.handlers.handler_contract_topic_reader import (
    HandlerContractTopicReader,
)

__all__ = ["load_seam_graph_facts"]

# Shapes whose topics a node consumes. COMMAND appears in both the
# subscribe and the command buckets; TOPICS_MAP counts nowhere.
_SUBSCRIBE_SHAPES: tuple[EnumContractTopicShape, ...] = (
    EnumContractTopicShape.EVENT_BUS_SUBSCRIBE,
    EnumContractTopicShape.CONSUMED_EVENTS,
    EnumContractTopicShape.HANDLER_ROUTING,
    EnumContractTopicShape.COMMAND,
)
_PUBLISH_SHAPES: tuple[EnumContractTopicShape, ...] = (
    EnumContractTopicShape.EVENT_BUS_PUBLISH,
    EnumContractTopicShape.TERMINAL,
    EnumContractTopicShape.PUBLISHED_EVENTS,
    EnumContractTopicShape.PROJECTION_API,
    EnumContractTopicShape.EVENT_BUS_DLQ,
)
_EXTERNAL_CONSUMED_SHAPES: tuple[EnumContractTopicShape, ...] = (
    EnumContractTopicShape.EXTERNALLY_CONSUMED,
)
_EXTERNAL_PRODUCED_SHAPES: tuple[EnumContractTopicShape, ...] = (
    EnumContractTopicShape.EXTERNALLY_PRODUCED,
)
_COMMAND_SHAPES: tuple[EnumContractTopicShape, ...] = (EnumContractTopicShape.COMMAND,)

# Node-name tokens a referencing file can carry.
_NODE_TOKEN_RE = re.compile(r"\bnode_[a-z0-9_]+\b")


class _LoadedContract(NamedTuple):
    """One contract file plus where its node lives on disk."""

    document: ModelContractDocument
    repo: str
    dirname: str
    node_dir: Path
    node_dir_relpath: str


class _ContractTopics(NamedTuple):
    """Topic buckets grouped from the topic reader's uses, per contract."""

    subscribe_topics: tuple[str, ...] = ()
    publish_topics: tuple[str, ...] = ()
    external_consumed_topics: tuple[str, ...] = ()
    external_produced_topics: tuple[str, ...] = ()
    command_topics: tuple[str, ...] = ()


def _python_files_under(directory: Path, rule: ModelSeamGraphRule) -> list[Path]:
    """Every ``*.py`` under directory, pruning excluded dir names, sorted.

    A directory that does not exist yields no files.
    """
    if not directory.is_dir():
        return []
    excluded = set(rule.excluded_dir_names)
    found: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(directory):
        dirnames[:] = sorted(name for name in dirnames if name not in excluded)
        found.extend(
            Path(dirpath) / name for name in sorted(filenames) if name.endswith(".py")
        )
    return found


def _read_python_source(path: Path) -> str | None:
    """File text with decoding errors replaced; None when unreadable."""
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def _yaml_top_level_keys(path: Path) -> tuple[str, ...]:
    """Sorted top-level string keys of a YAML mapping; empty otherwise."""
    if not path.is_file():
        return ()
    loader = getattr(yaml, "CSafeLoader", yaml.SafeLoader)
    try:
        with path.open("r", encoding="utf-8") as handle:
            data = yaml.load(handle, Loader=loader)
    except (yaml.YAMLError, UnicodeDecodeError, OSError):
        return ()
    if not isinstance(data, dict):
        return ()
    return tuple(sorted(key for key in data if isinstance(key, str)))


def _compile_markers(
    patterns: tuple[str, ...], kind: str
) -> tuple[re.Pattern[str], ...]:
    """Compile marker regexes, refusing an invalid one by name."""
    compiled: list[re.Pattern[str]] = []
    for pattern in patterns:
        try:
            compiled.append(re.compile(pattern))
        except re.error as error:
            raise ValueError(
                f"{kind} marker {pattern!r} is not a valid regex: {error}"
            ) from error
    return tuple(compiled)


def _resolve_repo_roots(
    rule: ModelSeamGraphRule, repo_roots: dict[str, str]
) -> dict[str, Path]:
    """Map repo name to checkout path, refusing a rule repo without one.

    The overlay repo is required too, unless it is itself one of the rule
    repos (and so already required).
    """
    roots = {name: Path(path) for name, path in repo_roots.items()}
    required = [repo.name for repo in rule.repos]
    if rule.overlay_repo not in {repo.name for repo in rule.repos}:
        required.append(rule.overlay_repo)
    for name in required:
        root = roots.get(name)
        if root is None:
            raise ValueError(f"repo {name!r} has no entry in repo_roots")
        if not root.is_dir():
            raise ValueError(f"repo {name!r} root {str(root)!r} is not a directory")
    return roots


def _load_one_contract(repo: str, root: Path, contract_path: Path) -> _LoadedContract:
    """Parse one contract file into a document plus its node location."""
    dirname = contract_path.parent.name
    node_dir = contract_path.parent
    document: ModelContractDocument
    loader = getattr(yaml, "CSafeLoader", yaml.SafeLoader)
    try:
        with contract_path.open("r", encoding="utf-8") as handle:
            data = yaml.load(handle, Loader=loader)
    except (yaml.YAMLError, UnicodeDecodeError, OSError) as error:
        document = ModelContractDocument(
            name=f"{repo}/{dirname}",
            path=contract_path.relative_to(root).as_posix(),
            read_error=str(error),
        )
    else:
        if isinstance(data, dict):
            document = ModelContractDocument(
                name=f"{repo}/{dirname}",
                path=contract_path.relative_to(root).as_posix(),
                document=data,
            )
        else:
            document = ModelContractDocument(
                name=f"{repo}/{dirname}",
                path=contract_path.relative_to(root).as_posix(),
                read_error="contract is not a mapping",
            )
    return _LoadedContract(
        document=document,
        repo=repo,
        dirname=dirname,
        node_dir=node_dir,
        node_dir_relpath=node_dir.relative_to(root).as_posix(),
    )


def _load_contract_documents(
    roots: dict[str, Path], rule: ModelSeamGraphRule
) -> tuple[_LoadedContract, ...]:
    """Glob and parse every contract file each repo rule names (step 1)."""
    loaded: list[_LoadedContract] = []
    for repo_rule in rule.repos:
        root = roots[repo_rule.name]
        for contract_path in sorted(root.glob(repo_rule.contract_glob)):
            loaded.append(_load_one_contract(repo_rule.name, root, contract_path))
    return tuple(loaded)


def _topics_for_shapes(
    by_shape: dict[EnumContractTopicShape, set[str]],
    shapes: tuple[EnumContractTopicShape, ...],
) -> tuple[str, ...]:
    """Sorted, de-duplicated topics of the given shapes."""
    topics: set[str] = set()
    for shape in shapes:
        topics.update(by_shape.get(shape, set()))
    return tuple(sorted(topics))


def _group_topic_uses(
    report: ModelContractTopicReport,
) -> dict[str, _ContractTopics]:
    """Group the topic reader's uses into per-contract buckets (step 1)."""
    by_contract: dict[str, dict[EnumContractTopicShape, set[str]]] = {}
    for use in report.uses:
        shapes = by_contract.setdefault(use.contract, {})
        shapes.setdefault(use.shape, set()).add(use.topic)
    return {
        contract: _ContractTopics(
            subscribe_topics=_topics_for_shapes(by_shape, _SUBSCRIBE_SHAPES),
            publish_topics=_topics_for_shapes(by_shape, _PUBLISH_SHAPES),
            external_consumed_topics=_topics_for_shapes(
                by_shape, _EXTERNAL_CONSUMED_SHAPES
            ),
            external_produced_topics=_topics_for_shapes(
                by_shape, _EXTERNAL_PRODUCED_SHAPES
            ),
            command_topics=_topics_for_shapes(by_shape, _COMMAND_SHAPES),
        )
        for contract, by_shape in by_contract.items()
    }


def _collect_overlay_facts(
    node_dir: Path, dirname: str, overlay_root: Path, rule: ModelSeamGraphRule
) -> tuple[bool, bool, tuple[str, ...], tuple[str, ...]]:
    """Overlay facts of one node (step 2), in field order:

    ``(needs_overlay, overlay_row_present, overlay_required_keys,
    overlay_row_keys)``.
    """
    needs_overlay = (node_dir / "overlay.py").is_file() or any(
        node_dir.glob("*overlay*.example.yaml")
    )
    overlay_required_keys = _yaml_top_level_keys(node_dir / "overlay.example.yaml")
    row_path = overlay_root / rule.overlay_row_glob.format(node=dirname)
    overlay_row_present = row_path.is_file()
    overlay_row_keys = _yaml_top_level_keys(row_path)
    return needs_overlay, overlay_row_present, overlay_required_keys, overlay_row_keys


def _collect_sync_io_hits(
    root: Path,
    node_dir: Path,
    rule: ModelSeamGraphRule,
    markers: tuple[re.Pattern[str], ...],
) -> tuple[str, ...]:
    """Sync-IO marker hits inside sync ``def handle`` bodies (step 3).

    Only ``ast.FunctionDef`` named ``handle`` counts — an async handler is
    not a sync-IO suspect. Files that fail to parse are skipped.
    """
    hits: set[str] = set()
    for path in _python_files_under(node_dir / "handlers", rule):
        source = _read_python_source(path)
        if source is None:
            continue
        try:
            tree = ast.parse(source)
        except (SyntaxError, ValueError):
            continue
        relpath = path.relative_to(root).as_posix()
        for item in ast.walk(tree):
            if not isinstance(item, ast.FunctionDef) or item.name != "handle":
                continue
            segment = ast.get_source_segment(source, item)
            if segment is None:
                continue
            for marker in markers:
                if marker.search(segment) is not None:
                    hits.add(f"{relpath}: {marker.pattern}")
    return tuple(sorted(hits))


def _collect_ledger_hits(
    root: Path,
    node_dir: Path,
    rule: ModelSeamGraphRule,
    markers: tuple[re.Pattern[str], ...],
) -> tuple[str, ...]:
    """Ledger-reader marker hits across a node's whole source (step 4)."""
    hits: set[str] = set()
    for path in _python_files_under(node_dir, rule):
        source = _read_python_source(path)
        if source is None:
            continue
        relpath = path.relative_to(root).as_posix()
        for marker in markers:
            if marker.search(source) is not None:
                hits.add(f"{relpath}: {marker.pattern}")
    return tuple(sorted(hits))


def _collect_file_references(
    path: Path,
    root: Path,
    repo: str,
    rule: ModelSeamGraphRule,
    known: dict[str, tuple[str, str]],
    referenced: dict[str, set[str]],
) -> None:
    """Add one scanned file's node references into the accumulator.

    A file naming more distinct nodes than the threshold allows is a
    catalog, never a caller. A file inside a node's own directory does not
    count as a reference to that node.
    """
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return
    tokens = set(_NODE_TOKEN_RE.findall(text)) & known.keys()
    if len(tokens) > rule.catalog_file_node_threshold:
        return
    file_relpath = path.relative_to(root).as_posix()
    for token in sorted(tokens):
        node_repo, node_dir_relpath = known[token]
        if node_repo == repo and file_relpath.startswith(node_dir_relpath + "/"):
            continue
        referenced[token].add(f"{repo}:{file_relpath}")


def _collect_references(
    roots: dict[str, Path],
    rule: ModelSeamGraphRule,
    known: dict[str, tuple[str, str]],
) -> dict[str, tuple[str, ...]]:
    """Files outside a node's own directory that name it (step 5).

    One walking pass per caller scan dir, shared by every node.
    """
    excluded = set(rule.excluded_dir_names)
    referenced: dict[str, set[str]] = {name: set() for name in known}
    for repo_rule in rule.repos:
        root = roots[repo_rule.name]
        for scan_dir in repo_rule.caller_scan_dirs:
            base = root / scan_dir
            if not base.is_dir():
                continue
            for dirpath, dirnames, filenames in os.walk(base):
                dirnames[:] = sorted(name for name in dirnames if name not in excluded)
                for filename in sorted(filenames):
                    path = Path(dirpath) / filename
                    if path.suffix not in rule.scanned_extensions:
                        continue
                    _collect_file_references(
                        path, root, repo_rule.name, rule, known, referenced
                    )
    return {name: tuple(sorted(refs)) for name, refs in referenced.items()}


def _cross_repo_imports(
    tree: ast.AST,
    importer_repo: str,
    importer_relpath: str,
    packages: dict[str, str],
) -> set[tuple[str, str, str, str]]:
    """Cross-repo imports one parsed file makes, as written.

    Yields ``(importer_repo, importer_relpath, module, owner_repo)`` —
    for ``from a.b import c`` the module is ``a.b``.
    """
    found: set[tuple[str, str, str, str]] = set()
    for item in ast.walk(tree):
        if isinstance(item, ast.Import):
            for alias in item.names:
                owner_repo = packages.get(alias.name.split(".")[0])
                if owner_repo is not None and owner_repo != importer_repo:
                    found.add((importer_repo, importer_relpath, alias.name, owner_repo))
        elif (
            isinstance(item, ast.ImportFrom)
            and item.level == 0
            and item.module is not None
        ):
            owner_repo = packages.get(item.module.split(".")[0])
            if owner_repo is not None and owner_repo != importer_repo:
                found.add((importer_repo, importer_relpath, item.module, owner_repo))
    return found


def _collect_import_records(
    roots: dict[str, Path],
    rule: ModelSeamGraphRule,
    packages: dict[str, str],
) -> tuple[tuple[str, str, str, str], ...]:
    """De-duplicated, sorted cross-repo import records (step 6)."""
    records: set[tuple[str, str, str, str]] = set()
    for repo_rule in rule.repos:
        root = roots[repo_rule.name]
        for path in _python_files_under(root / repo_rule.source_dir, rule):
            source = _read_python_source(path)
            if source is None:
                continue
            try:
                tree = ast.parse(source)
            except (SyntaxError, ValueError):
                continue
            relpath = path.relative_to(root).as_posix()
            records.update(_cross_repo_imports(tree, repo_rule.name, relpath, packages))
    return tuple(sorted(records))


def _collect_seam_test_constants(
    roots: dict[str, Path],
    rule: ModelSeamGraphRule,
    packages: dict[str, str],
) -> dict[str, frozenset[str]]:
    """Per repo, the string constants in its test dirs that name a package module.

    Only string literals count: a plain import statement is never a seam test,
    so ``import omnibase_core.x`` in a test contributes nothing.
    """
    constants: dict[str, set[str]] = {}
    for repo_rule in rule.repos:
        root = roots[repo_rule.name]
        found = constants.setdefault(repo_rule.name, set())
        for test_dir in repo_rule.test_dirs:
            for path in _python_files_under(root / test_dir, rule):
                source = _read_python_source(path)
                if source is None:
                    continue
                try:
                    tree = ast.parse(source)
                except SyntaxError:
                    continue
                for item in ast.walk(tree):
                    if not (
                        isinstance(item, ast.Constant) and isinstance(item.value, str)
                    ):
                        continue
                    value = item.value
                    if any(
                        value == package or value.startswith(package + ".")
                        for package in packages
                    ):
                        found.add(value)
    return {repo: frozenset(found) for repo, found in constants.items()}


def _collect_import_facts(
    roots: dict[str, Path], rule: ModelSeamGraphRule
) -> tuple[ModelSeamImportFact, ...]:
    """Cross-repo imports annotated with seam-test coverage (step 6).

    A seam test is a test of the OWNER repo that names the imported module as an
    exact string literal, so the owner's own suite fails when the module moves or
    is renamed. A literal in the importer's tests, or one naming a longer
    attribute path, does not count.
    """
    packages = {repo.package: repo.name for repo in rule.repos}
    constants = _collect_seam_test_constants(roots, rule, packages)
    return tuple(
        ModelSeamImportFact(
            importer_repo=importer_repo,
            importer_path=importer_path,
            module=module,
            owner_repo=owner_repo,
            has_seam_test=module in constants.get(owner_repo, frozenset()),
        )
        for importer_repo, importer_path, module, owner_repo in _collect_import_records(
            roots, rule, packages
        )
    )


def _node_type(document: ModelContractDocument) -> str:
    """Lower-cased ``node_type`` of a contract; ``unknown`` when unreadable."""
    if isinstance(document.document, dict):
        return str(document.document.get("node_type", "unknown")).lower()
    return "unknown"


def _build_node_facts(
    roots: dict[str, Path],
    rule: ModelSeamGraphRule,
    loaded_contracts: tuple[_LoadedContract, ...],
    topics_by_contract: dict[str, _ContractTopics],
    referenced_by: dict[str, tuple[str, ...]],
) -> tuple[ModelSeamNodeFact, ...]:
    """Assemble one fact per contract from steps 2-5, sorted by repo/name."""
    overlay_root = roots[rule.overlay_repo]
    sync_markers = _compile_markers(rule.sync_io_markers, "sync_io")
    ledger_markers = _compile_markers(rule.ledger_reader_markers, "ledger_reader")
    no_topics = _ContractTopics()
    nodes: list[ModelSeamNodeFact] = []
    for loaded in loaded_contracts:
        root = roots[loaded.repo]
        topics = topics_by_contract.get(loaded.document.name, no_topics)
        (
            needs_overlay,
            overlay_row_present,
            overlay_required_keys,
            overlay_row_keys,
        ) = _collect_overlay_facts(
            node_dir=loaded.node_dir,
            dirname=loaded.dirname,
            overlay_root=overlay_root,
            rule=rule,
        )
        nodes.append(
            ModelSeamNodeFact(
                repo=loaded.repo,
                name=loaded.dirname,
                contract_path=loaded.document.path,
                node_type=_node_type(loaded.document),
                subscribe_topics=topics.subscribe_topics,
                publish_topics=topics.publish_topics,
                external_consumed_topics=topics.external_consumed_topics,
                external_produced_topics=topics.external_produced_topics,
                command_topics=topics.command_topics,
                needs_overlay=needs_overlay,
                overlay_row_present=overlay_row_present,
                overlay_required_keys=overlay_required_keys,
                overlay_row_keys=overlay_row_keys,
                sync_io_hits=_collect_sync_io_hits(
                    root, loaded.node_dir, rule, sync_markers
                ),
                ledger_reader_hits=_collect_ledger_hits(
                    root, loaded.node_dir, rule, ledger_markers
                ),
                referenced_by=referenced_by.get(loaded.dirname, ()),
            )
        )
    return tuple(sorted(nodes, key=lambda node: (node.repo, node.name)))


def load_seam_graph_facts(request: ModelSeamGraphRequest) -> ModelSeamGraphFacts:
    """Gather raw seam-graph facts across the pinned checkouts of a rule.

    Refuses with ``ValueError`` naming the repo when a rule repo (or the
    overlay repo, when it is not itself a rule repo) has no checkout in
    ``repo_roots`` or its path is not a directory.
    """
    rule = request.rule
    roots = _resolve_repo_roots(rule, request.repo_roots)
    loaded_contracts = _load_contract_documents(roots, rule)
    report = HandlerContractTopicReader().handle(
        ModelContractTopicRequest(
            rule=ModelContractTopicRule(topic_pattern=rule.topic_pattern),
            documents=tuple(loaded.document for loaded in loaded_contracts),
        )
    )
    known = {
        loaded.dirname: (loaded.repo, loaded.node_dir_relpath)
        for loaded in loaded_contracts
    }
    return ModelSeamGraphFacts(
        nodes=_build_node_facts(
            roots=roots,
            rule=rule,
            loaded_contracts=loaded_contracts,
            topics_by_contract=_group_topic_uses(report),
            referenced_by=_collect_references(roots, rule, known),
        ),
        imports=_collect_import_facts(roots, rule),
        contracts_read=report.contracts_read,
        contracts_unreadable=tuple(
            sorted(unreadable.contract for unreadable in report.unreadable)
        ),
    )
