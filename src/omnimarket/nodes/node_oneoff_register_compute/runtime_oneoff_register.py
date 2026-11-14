# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""``python -m omnimarket.nodes.node_oneoff_register_compute``.

Read the one-off register against the liveness processes and the nodes that exist, and print the
reading's JSON on stdout.

Exit codes: 0 the register read passed; 1 the register read failed; 3 a deployment overlay is
missing or refuses to validate.
"""

from __future__ import annotations

import argparse
import pkgutil
import sys
import uuid
from collections.abc import Mapping, Sequence

from omnimarket.handlers.node_overlay_reader import NodeOverlayError
from omnimarket.nodes.node_oneoff_register_compute.handlers.handler_oneoff_register import (
    HandlerOneOffRegister,
)
from omnimarket.nodes.node_oneoff_register_compute.models.model_oneoff_register import (
    ModelLivenessOverlayRefs,
    ModelOneOffRegisterPolicy,
    ModelOneOffRegisterRequest,
    ModelOneOffRegisterResult,
)
from omnimarket.nodes.node_oneoff_register_compute.overlay import (
    load_liveness_refs,
    load_oneoff_register_policy,
)


def list_node_names() -> tuple[str, ...]:
    """The node packages under ``omnimarket.nodes``, sorted, with no names hard-coded."""
    import omnimarket.nodes as nodes

    return tuple(
        sorted(
            module.name
            for module in pkgutil.iter_modules(nodes.__path__)
            if module.name.startswith("node_")
        )
    )


def run_register(
    policy: ModelOneOffRegisterPolicy,
    liveness: ModelLivenessOverlayRefs,
    node_names: tuple[str, ...],
    correlation_id: str,
) -> ModelOneOffRegisterResult:
    """One register read: the request the node reads, through its handler."""
    return HandlerOneOffRegister().handle(
        ModelOneOffRegisterRequest(
            correlation_id=correlation_id,
            policy=policy,
            liveness_processes=liveness.processes,
            node_names=node_names,
        )
    )


def main(
    argv: Sequence[str] | None = None,
    environ: Mapping[str, str] | None = None,
) -> int:
    """Read the register and print its JSON: 0 on a pass, 1 on a fail, 3 when an overlay refuses."""
    parser = argparse.ArgumentParser(prog="node_oneoff_register_compute")
    parser.add_argument("--correlation-id", default=str(uuid.uuid4()))
    args = parser.parse_args(argv)

    try:
        policy = load_oneoff_register_policy(environ)
        liveness = load_liveness_refs(environ)
    except NodeOverlayError as exc:
        sys.stderr.write(f"oneoff-register: refused: {exc}\n")
        return 3

    result = run_register(policy, liveness, list_node_names(), args.correlation_id)
    sys.stdout.write(result.model_dump_json() + "\n")
    return 0 if result.passed else 1
