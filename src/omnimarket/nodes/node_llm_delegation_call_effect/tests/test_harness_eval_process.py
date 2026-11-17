# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Real child-process cwd isolation and compatibility for offline fixture runs.

Failure modes: the child inherits the operator project instead of its private
profile, a parent chdir leaks into concurrent work, or existing callers change.
"""

import os
import sys
from pathlib import Path

import pytest

from omnimarket.nodes.node_llm_delegation_call_effect import HandlerHarnessEvalProcess


@pytest.mark.parametrize("private_cwd", [False, True])
def test_fixture_process_uses_its_cwd_without_changing_parent(
    tmp_path: Path, private_cwd: bool
) -> None:
    before = Path.cwd()
    home = tmp_path / "private profile"
    home.mkdir()
    log = tmp_path / "process.log"
    marker = tmp_path.name
    command = [
        sys.executable,
        "-c",
        "import os,sys; print(os.getcwd()); print('child:'+sys.argv[1])",
        marker,
    ]
    run = HandlerHarnessEvalProcess().handle
    if private_cwd:
        code = run(command, env=dict(os.environ), timeout=10, log=log, cwd=home)
    else:
        code = run(command, env=dict(os.environ), timeout=10, log=log)
    assert code == 0
    assert log.read_text().splitlines() == [
        str(home if private_cwd else before),
        "child:" + marker,
    ]
    assert Path.cwd() == before
