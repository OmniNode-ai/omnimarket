# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The step order handler answers when the step runs it as a command (OMN-24507).

``delegation_step.py`` runs ``python -m omnimarket.handlers.delegation_step_order <kind> <class>`` and reads the last
line of stdout as JSON with ``choice`` and ``fields``. An answer is one JSON line. A refusing reader, an invalid
overlay, an order that places no engine, a host no engine is left on and an answer that does not validate print one
``STEP-ORDER-REFUSED`` line on stderr and exit 3, so the step never mistakes them for an answer or for a refused kind
(exit 1). The command reads the deployment's overlay roots, so the command tests run the module as a real subprocess
against their own overlay root, with no routing overlay bound.
"""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

import pytest
from pydantic import ValidationError

from omnimarket.handlers import delegation_step_order as mod
from omnimarket.handlers.delegation_routing_order_reader import (
    DelegationRoutingOrderError,
)
from omnimarket.handlers.node_overlay_reader import NodeOverlayError
from omnimarket.models.delegation.model_delegation_step_order import (
    ModelDelegationStepOrder,
)
from omnimarket.models.model_delegation_step_order_overlay import (
    ModelDelegationStepOrderOverlay,
)

pytestmark = pytest.mark.unit

LAB_FIRST = ("ladder", "claude-glm", "claude")
LAB_THEN_GLM = "code_engine_order: [lab, glm]\nengine_programs: {lab: null, glm: sh}\n"


def no_overlay() -> ModelDelegationStepOrderOverlay | None:
    """An install with no delegation step order overlay."""
    return None


def step_overlay(
    order: tuple[str, ...], programs: dict[str, str | None]
) -> Callable[[], ModelDelegationStepOrderOverlay]:
    """An install whose overlay orders ``order`` and names each engine's program."""
    value = ModelDelegationStepOrderOverlay(
        code_engine_order=order, engine_programs=programs
    )
    return lambda: value


def run_main(
    args: list[str],
    order: tuple[str, ...] | None = LAB_FIRST,
    overlay: Callable[[], ModelDelegationStepOrderOverlay | None] | None = None,
    has_program: Callable[[str], bool] | None = None,
) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    code = mod.main(
        args,
        read=lambda _task_class: order,
        overlay=overlay if overlay is not None else no_overlay,
        has_program=has_program,
        out=out,
        err=err,
    )
    return code, out.getvalue(), err.getvalue()


def answer_of(out: str) -> dict[str, object]:
    """The last stdout line as the JSON answer the step parses."""
    return json.loads(out.strip().splitlines()[-1])


def test_golden_text_answer_is_one_json_line_the_step_can_parse() -> None:
    code, out, err = run_main(["text", "document"])
    assert (code, err) == (0, "")
    assert answer_of(out) == {
        "choice": "lab",
        "fields": {
            "order_source": "routing_order_node",
            "routing_order": list(LAB_FIRST),
        },
    }


def test_a_code_step_answers_from_the_overlay_order() -> None:
    overlay = step_overlay(("lab", "glm"), {"lab": None, "glm": "claude"})
    code, out, err = run_main(
        ["code", "code_generation"], overlay=overlay, has_program=lambda _name: True
    )
    assert (code, err) == (0, "")
    assert answer_of(out) == {
        "choice": "last",
        "fields": {"order_source": "overlay", "routing_order": ["lab", "glm"]},
    }


def test_an_overlay_that_orders_glm_first_answers_before_lab() -> None:
    overlay = step_overlay(("glm", "lab"), {"lab": None, "glm": "claude"})
    code, out, _ = run_main(
        ["code", "code_generation"], overlay=overlay, has_program=lambda _name: True
    )
    assert code == 0
    assert answer_of(out)["choice"] == "before-lab"


def test_an_engine_the_host_lacks_is_skipped_and_named() -> None:
    overlay = step_overlay(("lab", "glm"), {"lab": None, "glm": "claude"})
    code, out, _ = run_main(
        ["code", "code_generation"], overlay=overlay, has_program=lambda _name: False
    )
    assert code == 0
    answer = answer_of(out)
    assert answer["choice"] == "last"
    fields = answer["fields"]
    assert isinstance(fields, dict)
    assert fields["engines_absent"] == ["glm"]


def test_error_no_engine_of_the_order_is_on_the_host() -> None:
    overlay = step_overlay(("glm",), {"lab": None, "glm": "claude"})
    code, out, err = run_main(
        ["code", "code_generation"], overlay=overlay, has_program=lambda _name: False
    )
    assert (code, out) == (3, "")
    assert err.startswith("STEP-ORDER-REFUSED reason=no_engine_on_host")


def test_error_a_refusing_reader_exits_3_with_a_typed_refusal_line() -> None:
    def refusing(_task_class: str) -> tuple[str, ...] | None:
        raise DelegationRoutingOrderError("no overlay root binds the order")

    out, err = io.StringIO(), io.StringIO()
    code = mod.main(
        ["text", "document"],
        read=refusing,
        overlay=no_overlay,
        out=out,
        err=err,
    )
    assert (code, out.getvalue()) == (3, "")
    assert err.getvalue().startswith("STEP-ORDER-REFUSED reason=reader_refused")
    assert "no overlay root binds the order" in err.getvalue()


def test_error_an_order_that_places_no_engine_exits_3() -> None:
    code, out, err = run_main(["text", "document"], order=("claude", "claude"))
    assert (code, out) == (3, "")
    assert err.startswith("STEP-ORDER-REFUSED reason=engines_not_placed")


def test_a_reader_with_no_order_keeps_the_text_default_and_names_the_class() -> None:
    code, out, _ = run_main(["text", "document"], order=None)
    assert code == 0
    answer = answer_of(out)
    assert answer["choice"] == "glm"
    fields = answer["fields"]
    assert isinstance(fields, dict)
    assert fields["order_source"] == "default"
    assert "document" in str(fields["order_fallback"])


def test_a_code_step_with_no_overlay_keeps_the_default() -> None:
    code, out, _ = run_main(["code", "code_generation"])
    assert code == 0
    answer = answer_of(out)
    assert answer["choice"] == "before-lab"
    fields = answer["fields"]
    assert isinstance(fields, dict)
    assert fields["order_source"] == "default"


def test_error_an_overlay_that_cannot_be_read_exits_3() -> None:
    def broken() -> ModelDelegationStepOrderOverlay | None:
        raise NodeOverlayError("bad")

    code, out, err = run_main(["code", "code_generation"], overlay=broken)
    assert (code, out) == (3, "")
    assert err.startswith("STEP-ORDER-REFUSED reason=overlay_invalid")
    assert "bad" in err


def test_error_an_answer_that_does_not_validate_exits_3_not_1(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A ValidationError is a refusal of the answer's shape, not a refused kind (exit 1)."""
    with pytest.raises(ValidationError) as caught:
        ModelDelegationStepOrder.model_validate({})
    broken_shape = caught.value

    def broken(
        kind: str,
        task_class: str,
        flag: str | None = None,
        *,
        read: Callable[[str], tuple[str, ...] | None] | None = None,
        overlay: Callable[[], ModelDelegationStepOrderOverlay | None] | None = None,
        has_program: Callable[[str], bool] | None = None,
    ) -> ModelDelegationStepOrder:
        raise broken_shape

    monkeypatch.setattr(mod, "step_order", broken)
    out, err = io.StringIO(), io.StringIO()
    assert mod.main(["text", "document"], out=out, err=err) == 3
    assert out.getvalue() == ""
    assert err.getvalue().startswith("STEP-ORDER-REFUSED reason=answer_shape")


def test_error_a_refused_kind_exits_1_with_no_json_line() -> None:
    code, out, err = run_main(["bogus", "document"])
    assert (code, out) == (1, "")
    assert "kind must be one of" in err


def test_error_a_wrong_argument_count_exits_2() -> None:
    code, out, err = run_main(["text"])
    assert (code, out) == (2, "")
    assert "usage:" in err


def command_env(root: Path) -> dict[str, str]:
    """The module's environment: the test's overlay root is the only one, no routing overlay is bound, and no workspace names a bus.

    With a workspace root the reader would ask the routing order node over the deployment's bus and answer from its overlay,
    so the workspace variables are dropped and the handler answers in this process.
    """
    dropped = {
        "ONEX_SKILL_OVERLAY_ROOTS",
        "DELEGATION_ROUTING_OVERLAY_PATH",
        "OMNIBASE_PATH",
        "OMNI_HOME",
        "ONEX_WORKSPACE_CONFIG_ROOT",
    }
    env = {name: value for name, value in os.environ.items() if name not in dropped}
    env["ONEX_SKILL_OVERLAY_ROOTS"] = str(root)
    return env


def write_overlay(root: Path, document: str) -> None:
    """Write the deployment's delegation step order overlay under ``root``."""
    step_dir = root / "delegation_step_order"
    step_dir.mkdir(parents=True, exist_ok=True)
    (step_dir / "overlay.yaml").write_text(document, encoding="utf-8")


def run_module(
    args: list[str], env: dict[str, str]
) -> subprocess.CompletedProcess[str]:
    """Run the handler as the step runs it: a real module in a real process."""
    return subprocess.run(
        [sys.executable, "-m", "omnimarket.handlers.delegation_step_order", *args],
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
        env=env,
    )


def test_the_module_runs_as_a_command_and_refuses_a_bad_kind(tmp_path: Path) -> None:
    """The control: before the entry point the module exited 0 and printed nothing for any argument."""
    write_overlay(tmp_path, LAB_THEN_GLM)
    done = run_module(["bogus", "document"], command_env(tmp_path))
    assert done.returncode == 1
    assert "kind must be one of" in done.stderr


def test_the_command_answers_the_code_order_from_a_real_overlay(tmp_path: Path) -> None:
    """The control: before the change the module exited 0 and printed nothing, so the step kept its old default."""
    write_overlay(tmp_path, LAB_THEN_GLM)
    done = run_module(["code", "code_generation"], command_env(tmp_path))
    assert done.returncode == 0
    answer = answer_of(done.stdout)
    assert answer["choice"] == "last"
    fields = answer["fields"]
    assert isinstance(fields, dict)
    assert fields["order_source"] == "overlay"


def test_the_command_names_an_engine_whose_program_the_host_lacks(
    tmp_path: Path,
) -> None:
    write_overlay(
        tmp_path,
        "code_engine_order: [lab, glm]\n"
        "engine_programs: {lab: null, glm: no-such-program-omn24507}\n",
    )
    done = run_module(["code", "code_generation"], command_env(tmp_path))
    assert done.returncode == 0
    answer = answer_of(done.stdout)
    assert answer["choice"] == "last"
    fields = answer["fields"]
    assert isinstance(fields, dict)
    assert fields["engines_absent"] == ["glm"]


def test_the_command_answers_glm_first_when_the_overlay_orders_it(
    tmp_path: Path,
) -> None:
    write_overlay(
        tmp_path,
        "code_engine_order: [glm, lab]\nengine_programs: {lab: null, glm: sh}\n",
    )
    done = run_module(["code", "code_generation"], command_env(tmp_path))
    assert done.returncode == 0
    assert answer_of(done.stdout)["choice"] == "before-lab"


def test_the_command_refuses_an_invalid_overlay(tmp_path: Path) -> None:
    write_overlay(
        tmp_path,
        "code_engine_order: []\nengine_programs: {lab: null, glm: sh}\n",
    )
    done = run_module(["code", "code_generation"], command_env(tmp_path))
    assert done.returncode == 3
    assert "STEP-ORDER-REFUSED reason=overlay_invalid" in done.stderr


def test_the_command_keeps_the_text_default_with_no_routing_overlay(
    tmp_path: Path,
) -> None:
    write_overlay(tmp_path, LAB_THEN_GLM)
    done = run_module(["text", "document"], command_env(tmp_path))
    assert done.returncode == 0
    answer = answer_of(done.stdout)
    assert answer["choice"] == "glm"
    fields = answer["fields"]
    assert isinstance(fields, dict)
    assert fields["order_source"] == "default"
    assert "order_fallback" in fields
