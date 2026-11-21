# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Starts the loop-hold sampler in a python process when the boot check asks for it.

The boot check mounts this directory into the kernel container and puts it on PYTHONPATH. Nothing runs unless
ONEX_BOOT_SAMPLER_OUT and ONEX_BOOT_SAMPLER_BOUND_SECONDS are set. A sitecustomize further down the path (the image may
carry one) is still run.
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))


def _chain_next_sitecustomize() -> None:
    for entry in sys.path:
        if os.path.abspath(entry or ".") == _HERE:
            continue
        candidate = os.path.join(entry or ".", "sitecustomize.py")
        if os.path.isfile(candidate):
            spec = importlib.util.spec_from_file_location(
                "_chained_sitecustomize", candidate
            )
            if spec is not None and spec.loader is not None:
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)
            return


def _start() -> None:
    out = os.environ.get("ONEX_BOOT_SAMPLER_OUT")
    bound = os.environ.get("ONEX_BOOT_SAMPLER_BOUND_SECONDS")
    if not out or not bound:
        return
    spec = importlib.util.spec_from_file_location(
        "loop_hold_sampler", os.path.join(_HERE, "loop_hold_sampler.py")
    )
    if spec is None or spec.loader is None:
        return
    module = importlib.util.module_from_spec(spec)
    sys.modules["loop_hold_sampler"] = module
    spec.loader.exec_module(module)

    fd = os.open(out, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)

    def sink(record: dict[str, object]) -> None:
        os.write(fd, (json.dumps(record) + "\n").encode("utf-8"))

    bound_seconds = float(bound)
    module.LoopHoldSampler(
        bound_seconds, min(0.1, bound_seconds / 10.0), sink
    ).install()


_chain_next_sitecustomize()
_start()
