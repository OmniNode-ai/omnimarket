# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A file that only names the ledger is no reader; a file that opens it still is (OMN-26318).

Each name-only shape has a real-reader twin that must stay counted.
"""

from __future__ import annotations

import pytest

from omnimarket.nodes.node_work_ledger_file_census_effect.handlers.classify import (
    executable_hits,
)

from .support import overlay

pytestmark = pytest.mark.unit

EXCLUDE_ONLY = (
    'LIVE_LEDGER_NAME = "@N@.md"\n'
    "def is_protected(relative):\n"
    "    return relative.name in (LIVE_LEDGER_NAME, MEMORY_FILE)\n"
)
EXCLUDE_THEN_OPEN = EXCLUDE_ONLY + (
    "def rows():\n    return open(LIVE_LEDGER_NAME).read()\n"
)
CHILD_ENV = (
    'def launch(request):\n    return run(cmd, env={"@V@": request.ledger_path})\n'
)
LITERAL_COMPARE = "def is_live(p):\n    return p.name == '@N@.md'\n"
LITERAL_OPEN = "def rows(d):\n    return (d / '@N@.md').read_text()\n"
CONST_PASSED_ON = 'NAME = "@N@.md"\ndef rows(d):\n    return load(d / NAME)\n'
ENV_READ = 'import os\ndef p():\n    return os.environ["@V@"]\n'


# The fixtures spell the names in two halves so this file is itself no reader of the ledger.
_NAME = "ROLLING_WORK_" + "LEDGER"
_VAR = "ONEX_LEDGER_" + "PATH"


def _hits(text: str) -> int:
    text = text.replace("@N@", _NAME).replace("@V@", _VAR)
    return len(executable_hits("src/x.py", text, overlay()))


@pytest.mark.parametrize("text", [EXCLUDE_ONLY, CHILD_ENV, LITERAL_COMPARE])
def test_name_only_file_is_not_counted(text: str) -> None:
    assert _hits(text) == 0


@pytest.mark.parametrize(
    "text", [EXCLUDE_THEN_OPEN, LITERAL_OPEN, CONST_PASSED_ON, ENV_READ]
)
def test_real_reader_is_still_counted(text: str) -> None:
    assert _hits(text) >= 1


LOOP_THEN_OPEN = 'for name in ("@V@",):\n    rows = open(os.environ[name]).read()\n'


def test_a_loop_over_the_name_is_still_counted() -> None:
    assert _hits(LOOP_THEN_OPEN) == 1
