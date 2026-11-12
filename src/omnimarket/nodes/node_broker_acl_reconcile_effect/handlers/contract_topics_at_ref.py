# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Read the topics the node contracts of a git revision declare (OMN-24005).

The coverage check compares the contracts as they stand with the contracts of the base branch; this reads the base side. The
reconcile effect owns the read (a subprocess to git); the coverage gate handler stays pure.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import yaml
from omnibase_core.validators.no_unguarded_git_subprocess import (
    scrub_git_location_env,
)

from omnimarket.models.broker_acl import ModelContractTopicUse
from omnimarket.nodes.node_broker_acl_reconcile_effect.handlers.contract_topics import (
    parse_topic_uses,
)


def _git(root: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(root), *args],
        capture_output=True,
        text=True,
        check=False,
        env=scrub_git_location_env(os.environ),
    )
    if completed.returncode != 0:
        raise ValueError(f"git {' '.join(args)}: {completed.stderr.strip()}")
    return completed.stdout


def load_topic_uses_at_ref(
    root: Path, ref: str, nodes_dir: str
) -> tuple[ModelContractTopicUse, ...]:
    """Every use of every <nodes_dir>/**/contract.yaml at <ref> in the repository at <root>; an unknown ref raises."""
    names = [
        name
        for name in _git(
            root, "ls-tree", "-r", "--name-only", ref, "--", nodes_dir
        ).splitlines()
        if name.endswith("/contract.yaml")
    ]
    uses: set[ModelContractTopicUse] = set()
    for name in names:
        try:
            uses.update(parse_topic_uses(_git(root, "show", f"{ref}:{name}")))
        except (ValueError, yaml.YAMLError) as error:
            raise ValueError(f"{ref}:{name}: {error}") from error
    return tuple(sorted(uses, key=lambda u: (u.node, u.direction.value, u.topic)))
