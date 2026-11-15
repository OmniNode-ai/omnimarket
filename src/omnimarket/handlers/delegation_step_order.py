# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The engine order of a delegation step (OMN-24044, OMN-24507).

A step asks for its first engine: ``text`` drafts on ``lab`` or ``glm`` first, and ``code`` runs GLM ``before-lab`` or
``last``. An explicit flag wins. Without one, a ``text`` step reads the routing overlay's chain for the class through
:func:`read_engine_order` (the reader names the lab tier ``ladder`` and a harness rung by its harness name) and runs on
the first lab or GLM engine the chain places; a ``code`` step takes the ``delegation_step_order`` overlay's
``code_engine_order`` (lab first then GLM is the deployed value) and never reads the chain. An engine whose
overlay-named program is not on the host's PATH is skipped and named in ``engines_absent`` on the receipt. A reader
that refuses, an overlay that is invalid, a chain that places no engine and a host that no engine is left on refuse
with :class:`DelegationStepOrderRefusalError` (the CLI prints one ``STEP-ORDER-REFUSED`` line and exits 3), never a
default, because a broken overlay must not quietly reorder a step. Only a missing order keeps the old default: a class
no routing chain names, or a ``code`` step no overlay names the order for, and the answer says why in
``order_fallback``.
"""

from __future__ import annotations

import json
import shutil
import sys
from collections.abc import Callable, Sequence
from typing import TextIO

from pydantic import ValidationError

from omnimarket.enums.enum_delegation_step_order_refusal import (
    EnumDelegationStepOrderRefusal,
)
from omnimarket.handlers.delegation_routing_order_reader import (
    DelegationRoutingOrderError,
    read_engine_order,
)
from omnimarket.handlers.delegation_step_order_overlay import (
    load_delegation_step_order_overlay,
)
from omnimarket.handlers.node_overlay_reader import NodeOverlayError
from omnimarket.models.delegation.model_delegation_step_order import (
    ModelDelegationStepOrder,
)
from omnimarket.models.model_delegation_step_order_overlay import (
    ModelDelegationStepOrderOverlay,
)

STEP_KINDS = ("text", "code")
OLD_DEFAULTS = {"text": "glm", "code": "before-lab"}
LAB_RUNG = "ladder"
GLM_RUNG = "claude-glm"


class DelegationStepOrderRefusalError(ValueError):
    """The step's engine order cannot be answered; ``refusal`` names the class of why."""

    def __init__(self, refusal: EnumDelegationStepOrderRefusal, detail: str) -> None:
        super().__init__(detail)
        self.refusal = refusal
        self.detail = detail

    def __str__(self) -> str:
        return self.detail


def _has_program(name: str) -> bool:
    """Whether ``name`` is a program on this host's PATH."""
    return shutil.which(name) is not None


def _rung_engines(order: tuple[str, ...]) -> tuple[str, ...]:
    """The engines a routing order places: ``ladder`` -> ``lab``, ``claude-glm`` -> ``glm``.

    A rung of another harness is passed over, and an engine already placed is not placed twice.
    """
    engines: list[str] = []
    for rung in order:
        engine = "lab" if rung == LAB_RUNG else "glm" if rung == GLM_RUNG else None
        if engine is not None and engine not in engines:
            engines.append(engine)
    return tuple(engines)


def _code_choice(engines: tuple[str, ...]) -> str:
    """``before-lab`` when GLM stands ahead of the lab tier or no lab tier remains, else ``last``."""
    if "glm" not in engines:
        return "last"
    glm_ahead = "lab" not in engines or engines.index("glm") < engines.index("lab")
    return "before-lab" if glm_ahead else "last"


def _absent_engines(
    engines: tuple[str, ...],
    overlay_value: ModelDelegationStepOrderOverlay,
    has_program: Callable[[str], bool],
) -> tuple[str, ...]:
    """The engines whose overlay-named program this host does not have, in order of appearance.

    An engine the overlay gives no program (``None``) is never absent.
    """
    programs: dict[str, str | None] = {
        "lab": overlay_value.engine_programs["lab"],
        "glm": overlay_value.engine_programs["glm"],
    }
    return tuple(
        engine
        for engine in engines
        if (program := programs[engine]) is not None and not has_program(program)
    )


def _engines_on_host(
    engines: tuple[str, ...],
    overlay_value: ModelDelegationStepOrderOverlay | None,
    has_program: Callable[[str], bool],
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """The engines left once the host's absent ones are dropped, and the absent ones.

    With no overlay no engine is checked: every engine stays. An order no engine is left
    from refuses with :class:`DelegationStepOrderRefusalError`.
    """
    absent = (
        _absent_engines(engines, overlay_value, has_program)
        if overlay_value is not None
        else ()
    )
    remaining = tuple(engine for engine in engines if engine not in absent)
    if not remaining:
        raise DelegationStepOrderRefusalError(
            EnumDelegationStepOrderRefusal.NO_ENGINE_ON_HOST,
            f"no engine of the order {list(engines)} is on this host",
        )
    return remaining, absent


def _text_step_order(
    task_class: str,
    overlay_value: ModelDelegationStepOrderOverlay | None,
    read: Callable[[str], tuple[str, ...] | None] | None,
    has_program: Callable[[str], bool],
) -> ModelDelegationStepOrder:
    """The text step's order: the routing chain's first lab or GLM engine the host has."""
    try:
        rungs = (read or read_engine_order)(task_class)
    except DelegationRoutingOrderError as exc:
        raise DelegationStepOrderRefusalError(
            EnumDelegationStepOrderRefusal.READER_REFUSED,
            f"routing order reader refused: {exc}",
        ) from None
    if rungs is None:
        return ModelDelegationStepOrder(
            choice=OLD_DEFAULTS["text"],
            order_source="default",
            order_fallback=f"the overlay names no engine order for {task_class}",
        )
    engines = _rung_engines(rungs)
    if not engines:
        raise DelegationStepOrderRefusalError(
            EnumDelegationStepOrderRefusal.ENGINES_NOT_PLACED,
            f"the overlay order {list(rungs)} does not place the engines",
        )
    remaining, absent = _engines_on_host(engines, overlay_value, has_program)
    return ModelDelegationStepOrder(
        choice=remaining[0],
        order_source="routing_order_node",
        routing_order=rungs,
        engines_absent=absent if absent else None,
    )


def _code_step_order(
    overlay_value: ModelDelegationStepOrderOverlay | None,
    has_program: Callable[[str], bool],
) -> ModelDelegationStepOrder:
    """The code step's order: the step order overlay's code engine order."""
    if overlay_value is None:
        return ModelDelegationStepOrder(
            choice=OLD_DEFAULTS["code"],
            order_source="default",
            order_fallback="no overlay names the code engine order",
        )
    engines = tuple(overlay_value.code_engine_order)
    remaining, absent = _engines_on_host(engines, overlay_value, has_program)
    return ModelDelegationStepOrder(
        choice=_code_choice(remaining),
        order_source="overlay",
        routing_order=engines,
        engines_absent=absent if absent else None,
    )


def step_order(
    kind: str,
    task_class: str,
    flag: str | None = None,
    *,
    read: Callable[[str], tuple[str, ...] | None] | None = None,
    overlay: Callable[[], ModelDelegationStepOrderOverlay | None] | None = None,
    has_program: Callable[[str], bool] | None = None,
) -> ModelDelegationStepOrder:
    """The order a ``kind`` step runs for ``task_class``: the flag, else the overlay's order, else the old default.

    ``overlay`` loads the step order overlay (the default reads the deployment's overlay roots);
    ``has_program`` says whether a program is on this host's PATH. A reader or overlay that refuses, and a
    host that no engine is left on, raise :class:`DelegationStepOrderRefusalError` rather than answer a default.
    """
    if kind not in STEP_KINDS:
        raise ValueError(f"kind must be one of {STEP_KINDS}, got {kind!r}")
    if flag is not None:
        return ModelDelegationStepOrder(choice=flag, order_source="flag")
    try:
        overlay_value = (overlay or load_delegation_step_order_overlay)()
    except NodeOverlayError as exc:
        raise DelegationStepOrderRefusalError(
            EnumDelegationStepOrderRefusal.OVERLAY_INVALID,
            f"the step order overlay is invalid: {exc}",
        ) from None
    check_program = has_program or _has_program
    if kind == "text":
        return _text_step_order(task_class, overlay_value, read, check_program)
    return _code_step_order(overlay_value, check_program)


def main(
    argv: Sequence[str] | None = None,
    *,
    read: Callable[[str], tuple[str, ...] | None] | None = None,
    overlay: Callable[[], ModelDelegationStepOrderOverlay | None] | None = None,
    has_program: Callable[[str], bool] | None = None,
    out: TextIO | None = None,
    err: TextIO | None = None,
) -> int:
    """Print the step order as one JSON line: ``{"choice": ..., "fields": {...}}``.

    This is how ``delegation_step.py`` asks (``python -m omnimarket.handlers.delegation_step_order <kind> <class>``)
    and what it parses from the last line of stdout: ``choice`` and the receipt ``fields``. Without this entry the
    module printed nothing and exited 0, the step read that as no answer and kept its old default, so a text step went
    to GLM first whatever the overlay's order said. A bad kind exits 1 with the reason on stderr and no JSON line. A
    refusal (a refusing reader, an invalid overlay, a host no engine is left on, or an answer that does not validate,
    ``answer_shape``) prints one ``STEP-ORDER-REFUSED`` line on stderr, nothing on stdout, and exits 3, so the step
    never mistakes it for an answer or for a refused kind.
    """
    stdout = out if out is not None else sys.stdout
    stderr = err if err is not None else sys.stderr
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 2:
        print("usage: delegation_step_order <text|code> <task class>", file=stderr)
        return 2
    kind, task_class = args
    if kind not in STEP_KINDS:
        print(f"kind must be one of {STEP_KINDS}, got {kind!r}", file=stderr)
        return 1
    try:
        answer = step_order(
            kind, task_class, read=read, overlay=overlay, has_program=has_program
        )
    except DelegationStepOrderRefusalError as exc:
        refusal = exc
    except ValidationError as exc:
        refusal = DelegationStepOrderRefusalError(
            EnumDelegationStepOrderRefusal.ANSWER_SHAPE, str(exc)
        )
    else:
        print(
            json.dumps({"choice": answer.choice, "fields": answer.receipt_fields()}),
            file=stdout,
        )
        return 0
    print(f"STEP-ORDER-REFUSED reason={refusal.refusal.value} {refusal}", file=stderr)
    return 3


if __name__ == "__main__":
    raise SystemExit(main())
