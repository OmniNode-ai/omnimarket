# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Files a closed lane's run folder must archive before its trees are removed.

This stdlib source is also inlined into the host payload, where Market need not
be installed. A lane saves unpushed git work as a bundle or a patch; no receipt or
ledger row names those files, so they are found by suffix under the run trees.
"""

import os
from pathlib import Path

# Result files the launcher writes into the lane's context directory.
_CONTEXT_RESULTS = ("result.txt", "result.json", "report.md")
# Suffixes of files that carry a lane's saved git work.
_SAVED_WORK_SUFFIXES = (".bundle", ".patch")


def _closeout_files(ctx: Path, trees: list[Path]) -> set[Path]:
    """Context result files plus every bundle and patch under the run trees.

    The walk stays inside each tree, follows no directory link, skips the git
    directory of a repository and skips a file that is itself a link.
    """
    found = {ctx / name for name in _CONTEXT_RESULTS if (ctx / name).exists()}
    for tree in trees:
        if tree.is_symlink() or not tree.is_dir():
            continue
        for directory, names, files in os.walk(tree):
            if ".git" in names:
                names.remove(".git")
            for name in files:
                path = Path(directory, name)
                if name.endswith(_SAVED_WORK_SUFFIXES) and not path.is_symlink():
                    found.add(path)
    return found
