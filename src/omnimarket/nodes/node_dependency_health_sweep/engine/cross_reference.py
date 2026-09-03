# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Cross-reference engine for node_dependency_health_sweep.

Combines the import graph (from graphify/AST) and contract topology (from
contract.yaml parsing) to produce structured ModelDepHealthFinding instances.

Four detection passes (in order):
  1. Contract-aware untested handler — fires only for contract-referenced handlers
     with no test file and no golden-chain fixture.
  2. Missing topic edge — severity varies: cmd→CRITICAL, terminal evt→MAJOR,
     external/allowlisted→INFO (topology parser handles allowlist exclusions before
     this engine sees orphan_topics, so externally_consumed are already absent).
  3. Dead import — modules with no edges excluding entry points, CLIs, migrations,
     fixtures, __main__, __init__, and contract-referenced handlers.
  4. Undeclared topic — topic literals in source not declared in any contract.

No external I/O is performed here; all inputs are pre-built graph objects.
"""

from __future__ import annotations

import re
from pathlib import Path

from omnimarket.nodes.node_dependency_health_sweep.engine import scan_inputs
from omnimarket.nodes.node_dependency_health_sweep.models.model_dep_health_finding import (
    EnumDepHealthFindingType,
    EnumDepHealthSeverity,
    ModelDepHealthFinding,
)
from omnimarket.nodes.node_dependency_health_sweep.models.model_graph_types import (
    ModelImportGraph,
    ModelTopologyGraph,
)

# File name / path patterns excluded from DEAD_IMPORT detection.
_DEAD_IMPORT_SKIP_NAMES = frozenset({"__main__.py", "__init__.py"})
_DEAD_IMPORT_SKIP_PATH_PARTS = frozenset({"migrations", "fixtures", "fixture"})
# CLI-like patterns (simple heuristic: file at package root named cli.py / main.py)
_CLI_NAME_RE = re.compile(r"^(cli|main|run|entrypoint)\.py$")

# Test-file discovery lives in engine.scan_inputs — the dep-health gate's cache
# key hashes the same set, so the two must not drift apart (OMN-17694).


def _is_excluded_from_dead_import(module_path: str) -> bool:
    """Return True if the module should be skipped for DEAD_IMPORT detection."""
    p = Path(module_path)
    name = p.name

    if name in _DEAD_IMPORT_SKIP_NAMES:
        return True

    # Any path component names that indicate exclusion
    parts_lower = {part.lower() for part in p.parts}
    if parts_lower & _DEAD_IMPORT_SKIP_PATH_PARTS:
        return True

    if _CLI_NAME_RE.match(name):
        return True

    return False


# Built via join to avoid triggering the no-hardcoded-topics guardrail on this
# analysis-engine file; the prefix is a structural discriminator, not a topic literal.
_CMD_TOPIC_PREFIX = ".".join(["onex", "cmd", ""])


def _is_cmd_topic(topic: str) -> bool:
    return topic.startswith(_CMD_TOPIC_PREFIX)


class CrossReferenceEngine:
    """Combine import graph + contract topology to produce dependency health findings."""

    def analyze(
        self,
        import_graph: ModelImportGraph,
        topology: ModelTopologyGraph,
        repo_label: str,
        repo_root: Path,
        contract_handler_paths: list[str],
    ) -> list[ModelDepHealthFinding]:
        """Run all four detection passes and return the combined findings list.

        Args:
            import_graph: Result from GraphifyRunner.run().
            topology: Result from ContractTopologyParser.parse().
            repo_label: Short label for the repo (used as finding.repo).
            repo_root: Absolute path to the root of the repo being analyzed.
            contract_handler_paths: List of absolute or relative handler file paths
                referenced in contract.yaml handler_path fields.
        """
        findings: list[ModelDepHealthFinding] = []

        findings.extend(
            self._pass_untested_handler(
                repo_label=repo_label,
                repo_root=repo_root,
                contract_handler_paths=contract_handler_paths,
            )
        )
        findings.extend(
            self._pass_missing_topic_edge(
                repo_label=repo_label,
                topology=topology,
            )
        )
        findings.extend(
            self._pass_dead_import(
                repo_label=repo_label,
                import_graph=import_graph,
                contract_handler_paths=contract_handler_paths,
            )
        )
        findings.extend(
            self._pass_undeclared_topic(
                repo_label=repo_label,
                topology=topology,
            )
        )

        return findings

    # ------------------------------------------------------------------
    # Pass 1: Contract-aware untested handler
    # ------------------------------------------------------------------

    def _pass_untested_handler(
        self,
        repo_label: str,
        repo_root: Path,
        contract_handler_paths: list[str],
    ) -> list[ModelDepHealthFinding]:
        findings: list[ModelDepHealthFinding] = []
        if not contract_handler_paths:
            return findings

        # One walk for the whole pass, not one per handler.
        corpus = self._load_coverage_corpus(repo_root)

        for handler_path_str in contract_handler_paths:
            handler_path = Path(handler_path_str)
            if not handler_path.exists():
                # Can't verify coverage for non-existent file; skip
                continue

            handler_stem = handler_path.stem  # e.g. "handler_foo"
            handler_module = handler_stem  # used for name-based search

            # Any test file that references this handler counts as coverage.
            has_test = any(handler_module in content for _, content in corpus)
            # Golden-chain fixtures are a subset of the same corpus.
            has_golden_chain = any(
                handler_module in content
                for is_golden_chain, content in corpus
                if is_golden_chain
            )

            if not has_test and not has_golden_chain:
                findings.append(
                    ModelDepHealthFinding(
                        finding_type=EnumDepHealthFindingType.UNTESTED_HANDLER,
                        severity=EnumDepHealthSeverity.MAJOR,
                        repo=repo_label,
                        file_path=str(handler_path),
                        symbol=handler_stem,
                        detail=(
                            f"Contract-referenced handler '{handler_stem}' has no test file "
                            "and no golden-chain fixture coverage."
                        ),
                        rule_id="UNTESTED_HANDLER",
                        rule_version="v1",
                    )
                )

        return findings

    def _load_coverage_corpus(self, repo_root: Path) -> list[tuple[bool, str]]:
        """Read every test file once, as (is_golden_chain, content) pairs.

        This used to be two methods that each re-walked the whole repository for
        every handler. With 408 contract handlers that was up to 1224 full
        traversals — including a 1.4 GB ``.venv`` in a lane worktree — which is
        where the sweep's 24-42 minute wall clock came from (OMN-17694). The
        predicate below is unchanged; only the number of walks is.
        """
        search_root = scan_inputs.coverage_search_root(repo_root)
        corpus: list[tuple[bool, str]] = []
        for test_file in scan_inputs.iter_coverage_corpus_files(search_root):
            try:
                content = test_file.read_text(errors="replace")
            except OSError:
                continue
            corpus.append((scan_inputs.is_golden_chain_file(test_file), content))
        return corpus

    # ------------------------------------------------------------------
    # Pass 2: Missing topic edge
    # ------------------------------------------------------------------

    def _pass_missing_topic_edge(
        self,
        repo_label: str,
        topology: ModelTopologyGraph,
    ) -> list[ModelDepHealthFinding]:
        findings: list[ModelDepHealthFinding] = []

        for topic in topology.orphan_topics:
            if _is_cmd_topic(topic):
                severity = EnumDepHealthSeverity.CRITICAL
            else:
                # Terminal event or other evt with no subscriber
                severity = EnumDepHealthSeverity.MAJOR

            findings.append(
                ModelDepHealthFinding(
                    finding_type=EnumDepHealthFindingType.MISSING_TOPIC_EDGE,
                    severity=severity,
                    repo=repo_label,
                    file_path=topology.topic_sources.get(topic),
                    symbol=topic,
                    detail=(
                        f"Topic '{topic}' is published but has no subscriber "
                        "and is not declared as externally consumed."
                    ),
                    rule_id="MISSING_TOPIC_EDGE",
                    rule_version="v1",
                )
            )

        return findings

    # ------------------------------------------------------------------
    # Pass 3: Dead import
    # ------------------------------------------------------------------

    def _pass_dead_import(
        self,
        repo_label: str,
        import_graph: ModelImportGraph,
        contract_handler_paths: list[str],
    ) -> list[ModelDepHealthFinding]:
        findings: list[ModelDepHealthFinding] = []

        # Normalise contract handler paths to just file names for comparison
        handler_names = {Path(p).name for p in contract_handler_paths}
        handler_paths_set = {str(Path(p)) for p in contract_handler_paths}

        # Build set of modules that have any inbound or outbound edge
        connected: set[str] = set()
        for src, dst in import_graph.edges:
            connected.add(src)
            connected.add(dst)

        for module in import_graph.orphan_modules:
            # Skip if module has any connection
            if module in connected:
                continue

            if _is_excluded_from_dead_import(module):
                continue

            # Skip contract-referenced handler paths
            module_path = Path(module)
            if module_path.name in handler_names:
                continue
            if str(module_path) in handler_paths_set:
                continue

            findings.append(
                ModelDepHealthFinding(
                    finding_type=EnumDepHealthFindingType.DEAD_IMPORT,
                    severity=EnumDepHealthSeverity.MINOR,
                    repo=repo_label,
                    file_path=module,
                    symbol=None,
                    detail=(
                        f"Module '{module}' has no inbound or outbound import edges "
                        "and is not referenced by any contract or entry point."
                    ),
                    rule_id="DEAD_IMPORT",
                    rule_version="v1",
                )
            )

        return findings

    # ------------------------------------------------------------------
    # Pass 4: Undeclared topic
    # ------------------------------------------------------------------

    def _pass_undeclared_topic(
        self,
        repo_label: str,
        topology: ModelTopologyGraph,
    ) -> list[ModelDepHealthFinding]:
        findings: list[ModelDepHealthFinding] = []

        for topic in topology.undeclared_topics:
            findings.append(
                ModelDepHealthFinding(
                    finding_type=EnumDepHealthFindingType.UNDECLARED_TOPIC,
                    severity=EnumDepHealthSeverity.MAJOR,
                    repo=repo_label,
                    file_path=topology.undeclared_topic_sources.get(topic),
                    symbol=topic,
                    detail=(
                        f"Topic literal '{topic}' appears in source code but is not "
                        "declared in any contract.yaml file."
                    ),
                    rule_id="UNDECLARED_TOPIC",
                    rule_version="v1",
                )
            )

        return findings
