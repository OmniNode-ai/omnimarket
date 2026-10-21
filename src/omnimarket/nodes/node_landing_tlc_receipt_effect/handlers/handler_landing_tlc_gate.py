# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Load the landing controller's own version gate and put one TLC receipt through it.

The landing controller's gate script is supplied with ``--gate-script``. Its ``verify`` command wants all
three G-ACT receipts, and only the model-check receipt is built here, so the model-check branch of
that command is run entry point by entry point, in the order ``verify`` runs them. The rules stay
the gate's: this module holds none of its own.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

TAG_PREFIX = "landing-tlc-"
PIN_DIR_PREFIX = "g-act-"
# The gate refuses model-check output that ran on a developer laptop; the marker is its own default.
LOCAL_PLATFORM_MARKER = "Darwin"
_ENTRY_POINTS = (
    "_load_receipt",
    "_validate_receipt_shape",
    "_check_binding",
    "_verified_evidence",
    "_verify_tlc_claims",
)


def load_gate(gate_script: Path) -> ModuleType:
    """Import the gate file by path; refuse a file that lacks an entry point this module calls."""
    spec = importlib.util.spec_from_file_location("landing_tlc_gate", gate_script)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load the landing gate at {gate_script}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    missing = [name for name in _ENTRY_POINTS if not hasattr(module, name)]
    if missing:
        raise ImportError(
            f"the landing gate at {gate_script} has no {', '.join(missing)}"
        )
    return module


def check_tlc_receipt(gate: ModuleType, receipt_dir: Path, pin: str) -> list[str]:
    """The reasons the gate refuses ``receipt_dir/tlc.json`` for ``pin``; empty when it accepts."""
    reasons: list[str] = []
    receipt = vars(gate)["_load_receipt"](receipt_dir / "tlc.json", "tlc", reasons)
    if receipt is None:
        return reasons
    shape_ok = vars(gate)["_validate_receipt_shape"](receipt, "tlc", "tlc", reasons)
    vars(gate)["_check_binding"](receipt, "tlc", pin, None, reasons)
    if not shape_ok:
        return reasons
    paths = vars(gate)["_verified_evidence"](receipt, receipt_dir, "tlc", reasons)
    if paths is not None:
        vars(gate)["_verify_tlc_claims"](receipt, paths, LOCAL_PLATFORM_MARKER, reasons)
    return reasons
