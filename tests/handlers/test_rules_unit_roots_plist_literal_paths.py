# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The launchd plist reader keeps a literal program or directory path whole (Tessl on omnimarket#5273).

A structured launchd string such as ``WorkingDirectory`` is one path, not command text: scanning it with
the command-text pattern cut it at a space or a comma, so a worktree behind a space was missed and a
path with a comma was refused for a segment it does not have. Environment path lists and embedded
shell commands are still read by their separators.
"""

from __future__ import annotations

import plistlib

import pytest

from omnimarket.handlers.rules_unit_roots import (
    UnitRootRefusedError,
    plist_paths,
    refuse_worktree_plists,
)

SPACED = "/mnt/second disk/omni_worktrees/OMN-1/clone"
COMMA = "/srv/omni_worktrees,archive/clone"


@pytest.mark.parametrize("key", ["WorkingDirectory", "RootDirectory", "Program"])
def test_a_literal_path_with_a_space_is_kept_whole_and_refused(key: str) -> None:
    data = plistlib.dumps({key: SPACED})
    assert plist_paths(data) == {SPACED}
    with pytest.raises(UnitRootRefusedError) as raised:
        refuse_worktree_plists({"x.plist": data})
    assert raised.value.paths == (SPACED,)


def test_a_program_argument_path_with_a_space_is_kept_whole() -> None:
    data = plistlib.dumps({"ProgramArguments": [SPACED, "-m", "x"]})
    assert plist_paths(data) == {SPACED}


def test_a_literal_path_with_a_comma_is_not_cut_into_a_worktree_segment() -> None:
    data = plistlib.dumps({"WorkingDirectory": COMMA})
    assert plist_paths(data) == {COMMA}
    refuse_worktree_plists({"x.plist": data})


def test_environment_path_lists_and_embedded_commands_are_still_read() -> None:
    data = plistlib.dumps(
        {
            "ProgramArguments": ["/bin/sh", "-c", "cd /a/omni_worktrees/t && run /b/x"],
            "EnvironmentVariables": {"PATH": "/usr/bin:/opt/omni_worktrees/t/bin"},
        }
    )
    assert plist_paths(data) == {
        "/bin/sh",
        "/a/omni_worktrees/t",
        "/b/x",
        "/usr/bin",
        "/opt/omni_worktrees/t/bin",
    }
