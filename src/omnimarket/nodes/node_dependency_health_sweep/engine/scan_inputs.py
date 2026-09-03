# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-17694: the single definition of the file set the dep-health sweep reads.

Two consumers must agree on this set exactly:

* the sweep engine, which opens these files to produce findings;
* ``scripts/validation/dep_health_gate_cache.py``, whose cache key is a hash of
  them — a key computed over a *different* set than the sweep reads would serve
  a "hit" that is not a verdict for the inputs at hand.

Keeping the definition here, and importing it from both sides, is what makes
"this cache hit is for these exact inputs" a property of the code rather than a
convention two files happen to share.

Stdlib only, and free of intra-package imports, on purpose: the pre-commit
wrapper loads this module by file path under the bare system ``python3``, with
no ``uv run`` and no ``omnimarket`` package import.
"""

from __future__ import annotations

import fnmatch
import os
from pathlib import Path

# Suffixes the topic-literal scan opens (ContractTopologyParser._SOURCE_EXTENSIONS),
# which is a superset of the ``.py`` files the import-graph scanner parses.
SCANNED_SUFFIXES = frozenset({".py", ".ts", ".tsx", ".yaml", ".yml"})

# Filename globs the contract-handler coverage passes read
# (CrossReferenceEngine._pass_untested_handler).
TEST_FILE_GLOBS = ("test_*.py", "*_test.py")
GOLDEN_CHAIN_GLOB = "test_golden_chain_*.py"

# Directories that hold no first-party source and are never coverage for an
# omnimarket handler: dependency trees, build output, VCS and tool caches.
#
# Pruning them is not an optimisation detail — a vendored package's ``test_*.py``
# matching a handler stem would count as coverage for that handler, and the
# handler-coverage pass used to walk a 1.4 GB ``.venv`` once per handler.
NON_SOURCE_DIR_NAMES = frozenset(
    {
        ".git",
        ".venv",
        "venv",
        ".tox",
        ".nox",
        ".eggs",
        "node_modules",
        "__pycache__",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".hypothesis",
        "htmlcov",
        "site-packages",
    }
)


def iter_scanned_source_files(src_root: Path) -> list[Path]:
    """Return every file under ``src_root`` the sweep opens, sorted.

    Deliberately unpruned: this mirrors ``ContractTopologyParser`` and the
    import-graph scanner, which both ``rglob`` the source root with no
    exclusions. The set is the sweep's, not an idealised one.
    """
    if not src_root.is_dir():
        return []
    return sorted(
        path
        for path in src_root.rglob("*")
        if path.suffix in SCANNED_SUFFIXES and path.is_file()
    )


def iter_coverage_corpus_files(search_root: Path) -> list[Path]:
    """Return every test file the handler-coverage passes read, from one walk.

    One ``os.walk`` replaces the per-handler ``rglob`` the coverage passes used
    to run: with 408 contract handlers that was up to 1224 full traversals of
    the repository for a single sweep.
    """
    if not search_root.is_dir():
        return []
    found: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(search_root):
        dirnames[:] = [name for name in dirnames if name not in NON_SOURCE_DIR_NAMES]
        for name in filenames:
            if any(fnmatch.fnmatchcase(name, glob) for glob in TEST_FILE_GLOBS):
                found.append(Path(dirpath) / name)
    return sorted(found)


def is_golden_chain_file(path: Path) -> bool:
    """Return True when ``path`` is one of the golden-chain fixtures."""
    return fnmatch.fnmatchcase(path.name, GOLDEN_CHAIN_GLOB)


def coverage_search_root(repo_root: Path) -> Path:
    """Return the root the coverage passes search for a given analysed root.

    A sweep run against ``<repo>/src`` searches ``<repo>`` for tests, because
    the tests live beside ``src/``, not inside it.
    """
    return repo_root.parent if repo_root.name == "src" else repo_root
