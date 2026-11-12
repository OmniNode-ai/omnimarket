# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Local run of the effect-backends validator (OMN-23114).

Every operation the two node contracts advertise (the check node's ``effect_backends`` and the load
node's ``load_contracts``) is dispatched from the contracts' own ``handler_routing`` tables, so
``load_contracts`` is reached by a local run and not only advertised::

    python -m omnimarket.nodes.node_effect_backends_load_effect.runtime_effect_backends \
        --contract <check node contract.yaml> --contract <load node contract.yaml> <root>

The contracts reach the runtime through the caller, never located from this module. ``<root>`` is a directory of node folders, each holding a ``contract.yaml``. The report is printed as
JSON. The exit code is 0 for a scanned inventory and 2 when the root is unavailable.
"""

from __future__ import annotations

import argparse
import importlib
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import cast

from omnibase_core.utils.util_safe_yaml_loader import load_yaml_mapping_no_duplicates

from omnimarket.models.model_effect_backends_report import (
    ModelEffectBackendsReport,
)
from omnimarket.models.model_effect_backends_request import (
    ModelEffectBackendsRequest,
)
from omnimarket.nodes.node_effect_backends_check_compute.handlers.handler_effect_backends import (
    HandlerEffectBackends,
)
from omnimarket.nodes.node_effect_backends_load_effect.handlers.handler_effect_backends_loader import (
    HandlerEffectBackendsLoader,
)
from omnimarket.nodes.node_effect_backends_load_effect.models.model_effect_backends_load_request import (
    ModelEffectBackendsLoadRequest,
)
from omnimarket.nodes.node_effect_backends_load_effect.models.model_loaded_contracts import (
    ModelLoadedContracts,
)

LOAD_OPERATION = "load_contracts"
JUDGE_OPERATION = "effect_backends"
EXIT_UNAVAILABLE = 2


def dispatch_table(contract_paths: Sequence[Path]) -> dict[str, object]:
    """One handler instance per operation of the contracts' ``handler_routing`` tables."""
    table: dict[str, object] = {}
    for contract_path in contract_paths:
        contract = load_yaml_mapping_no_duplicates(
            contract_path.read_text(encoding="utf-8"), source=str(contract_path)
        )
        routing = cast(Mapping[str, object], contract["handler_routing"])
        for raw_entry in cast(Sequence[Mapping[str, object]], routing["handlers"]):
            handler = cast(Mapping[str, str], raw_entry["handler"])
            cls = getattr(importlib.import_module(handler["module"]), handler["name"])
            table[str(raw_entry["operation"])] = cls()
    return table


def run_local(root: str, contract_paths: Sequence[Path]) -> ModelEffectBackendsReport:
    """Load the contracts under ``root`` then judge them, both through the dispatch table."""
    table = dispatch_table(contract_paths)
    loader = cast(HandlerEffectBackendsLoader, table[LOAD_OPERATION])
    judge = cast(HandlerEffectBackends, table[JUDGE_OPERATION])
    loaded: ModelLoadedContracts = loader.handle(
        ModelEffectBackendsLoadRequest(root=root)
    )
    return judge.handle(
        ModelEffectBackendsRequest(
            contracts=loaded.documents,
            unreadable=loaded.unreadable,
            root_unavailable=loaded.root_unavailable,
        )
    )


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point; prints the report as JSON."""
    parser = argparse.ArgumentParser(
        description="Effect-backends validator (OMN-22911)"
    )
    parser.add_argument(
        "--contract",
        required=True,
        action="append",
        type=Path,
        help="A node contract.yaml whose handler_routing is dispatched; the check and the load node",
    )
    parser.add_argument("root", help="Directory whose children are node folders")
    args = parser.parse_args(list(argv) if argv is not None else None)
    report = run_local(args.root, args.contract)
    sys.stdout.write(f"{report.model_dump_json(indent=2)}\n")
    return 0 if report.inventory_available else EXIT_UNAVAILABLE


if __name__ == "__main__":
    sys.exit(main())
