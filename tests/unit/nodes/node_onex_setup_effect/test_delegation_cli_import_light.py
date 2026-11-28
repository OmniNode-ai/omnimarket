# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""`onex delegation` loads only what switching needs (OMN-26688).

Operator 2026-11-26: "turng on off takes time". Measured that day on the
operator's Mac: `onex delegation status` took 2.4-3.1 s, most of it importing
the delegation commands, which pulled the inference stack (19 modules) and
omnibase_infra (519) in through one small file-read helper.

Failure mode:
  S1  importing the delegation commands imports the inference stack or
      omnibase_infra.
"""

from __future__ import annotations

import subprocess
import sys

import pytest

pytestmark = pytest.mark.unit

_HEAVY = ("omnimarket.inference", "omnibase_infra")


def test_the_delegation_commands_do_not_import_the_inference_stack() -> None:
    """S1."""
    code = (
        "import sys\n"
        "import omnimarket.nodes.node_onex_setup_effect.cli_delegation\n"
        f"print(sorted(m for m in sys.modules if m.startswith({_HEAVY!r})))\n"
    )
    out = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        timeout=120,
        check=True,
    ).stdout
    assert out.strip() == "[]", out[:500]
