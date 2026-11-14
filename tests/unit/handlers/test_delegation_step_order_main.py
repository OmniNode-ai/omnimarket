# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The step order handler answers when the step runs it as a command (OMN-24507).

``delegation_step.py`` runs ``python -m omnimarket.handlers.delegation_step_order <kind> <class>`` and reads the last
line of stdout as JSON with ``choice`` and ``fields``. The module had no entry point, so the run printed nothing and
exited 0 and the step kept its old default (GLM first) whatever the overlay's order said.
"""

from __future__ import annotations

import io
import json
import subprocess
import sys

import pytest

from omnimarket.handlers import delegation_step_order as mod
from omnimarket.handlers.delegation_routing_order_reader import (
    DelegationRoutingOrderError,
)

pytestmark = pytest.mark.unit

LAB_FIRST = ("ladder", "claude-glm", "claude")


def run_main(args: list[str], order: tuple[str, ...] | None) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    code = mod.main(args, read=lambda _task_class: order, out=out, err=err)
    return code, out.getvalue(), err.getvalue()


def test_golden_text_answer_is_one_json_line_the_step_can_parse() -> None:
    code, out, err = run_main(["text", "document"], LAB_FIRST)
    assert (code, err) == (0, "")
    answer = json.loads(out.strip().splitlines()[-1])
    assert answer == {
        "choice": "lab",
        "fields": {
            "order_source": "routing_order_node",
            "routing_order": list(LAB_FIRST),
        },
    }


def test_golden_code_answer_names_the_glm_position() -> None:
    code, out, _ = run_main(["code", "code_generation"], ("claude-glm", "ladder"))
    assert code == 0
    assert json.loads(out)["choice"] == "before-lab"


def test_a_reader_that_refuses_keeps_the_default_and_says_why() -> None:
    def refusing(_task_class: str) -> tuple[str, ...] | None:
        raise DelegationRoutingOrderError("no overlay root binds the order")

    out, err = io.StringIO(), io.StringIO()
    assert mod.main(["text", "document"], read=refusing, out=out, err=err) == 0
    answer = json.loads(out.getvalue())
    assert answer["choice"] == "glm"
    assert answer["fields"]["order_source"] == "default"
    assert "no overlay root binds the order" in answer["fields"]["order_fallback"]


def test_error_a_refused_kind_exits_1_with_no_json_line() -> None:
    code, out, err = run_main(["bogus", "document"], LAB_FIRST)
    assert (code, out) == (1, "")
    assert "kind must be one of" in err


def test_error_a_wrong_argument_count_exits_2() -> None:
    code, out, err = run_main(["text"], LAB_FIRST)
    assert (code, out) == (2, "")
    assert "usage:" in err


def test_the_module_runs_as_a_command_and_refuses_a_bad_kind() -> None:
    """The control: before the entry point the module exited 0 and printed nothing for any argument."""
    done = subprocess.run(
        [
            sys.executable,
            "-m",
            "omnimarket.handlers.delegation_step_order",
            "bogus",
            "document",
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )
    assert done.returncode == 1
    assert "kind must be one of" in done.stderr
