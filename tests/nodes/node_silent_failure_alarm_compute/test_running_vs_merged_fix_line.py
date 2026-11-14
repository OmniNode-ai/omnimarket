# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The plugin-behind repair line names only flags the install-tree command accepts (OMN-19998).

The seam: the install record's version, read by the host-drift fold as ``plugin-behind:`` /
``plugin-stale:``, becomes this alarm's FIX line, which an operator or a lane runs through the adopt
tool's install-tree command. That command's parser has no ``--lane`` flag; the line the frozen copy
printed ended ``--lane <host>-by-hand`` and the tool refused it. ``ACCEPTED_FLAGS`` spells the flags
that parser declares (``install-tree``: --branch, --commit, --clone, --version-dir, --dry-run,
--with-installed, --ledger, --no-ledger, --target, read on h053 from the plugin's installed
adopt_merged.py), and the check below is the same one a refusal would be: a flag outside the list.
"""

from __future__ import annotations

import re
from datetime import datetime

import pytest

from omnimarket.models.model_running_vs_merged_alarm import (
    ModelHostOmniHome,
    ModelRunningVsMergedFinding,
)
from omnimarket.nodes.node_silent_failure_alarm_compute.handlers.handler_running_vs_merged_alarm import (
    HandlerRunningVsMergedAlarm,
    request_from_fleet,
)

ACCEPTED_FLAGS = frozenset(
    {
        "--branch",
        "--commit",
        "--clone",
        "--version-dir",
        "--dry-run",
        "--with-installed",
        "--ledger",
        "--no-ledger",
        "--target",
    }
)
NOW = datetime.fromisoformat("2026-11-14T03:00:00+00:00")
FROZEN_LINE = (
    'on h101: python3 "$(ls -d ~/.claude/plugins/cache/omninode-internal/omni/*/skills/adopt-merged'
    ' | sort -V | tail -1)/scripts/adopt_merged.py" install-tree --branch main --with-installed'
    " --lane h101-by-hand (the host-drift install-plugin-tree action)"
)


def install_tree_flags(fix: str) -> list[str]:
    """The flags after ``install-tree`` in a fix line, up to its parenthesised note."""
    tail = fix.split("install-tree", 1)[1].split(" (", 1)[0]
    return re.findall(r"--[a-z][a-z-]*", tail)


def _fix(
    observed: str, host: str = "h101", homes: tuple[ModelHostOmniHome, ...] = ()
) -> str:
    fleet = {
        "folded_at": "2026-11-14T03:00:00Z",
        "hosts": [
            {
                "host": host,
                "open": [
                    {
                        "key": "plugin:omni@omninode-internal",
                        "kind": "plugin",
                        "observed": observed,
                        "first_seen_at": "2026-11-14T00:00:00Z",
                        "verdict": "DRIFT",
                    }
                ],
            }
        ],
    }
    request = request_from_fleet(fleet, NOW).model_copy(update={"host_homes": homes})
    finding: ModelRunningVsMergedFinding = (
        HandlerRunningVsMergedAlarm().handle(request).findings[0]
    )
    return finding.fix


@pytest.mark.parametrize(
    "observed", ["plugin-behind: installed 0.4.477", "plugin-stale: 0.4.470"]
)
def test_the_line_uses_only_flags_the_install_tree_command_accepts(
    observed: str,
) -> None:
    flags = install_tree_flags(_fix(observed))
    assert flags == ["--branch", "--with-installed"]
    assert set(flags) <= ACCEPTED_FLAGS
    assert "by-hand" not in _fix(observed)


def test_the_line_carries_the_hosts_declared_home() -> None:
    homes = (ModelHostOmniHome(host="h101", home="/srv/registry"),)
    assert "OMNI_HOME=/srv/registry python3" in _fix("plugin-behind: x", homes=homes)


def test_a_host_with_no_declared_row_reads_its_own_environment() -> None:
    homes = (ModelHostOmniHome(host="h201", home="/srv/registry"),)
    assert "OMNI_HOME=$OMNI_HOME python3" in _fix(
        "plugin-behind: x", host="h101", homes=homes
    )


def test_failing_control_the_frozen_line_is_refused_by_the_same_check() -> None:
    assert "--lane" in install_tree_flags(FROZEN_LINE)
    assert not set(install_tree_flags(FROZEN_LINE)) <= ACCEPTED_FLAGS
