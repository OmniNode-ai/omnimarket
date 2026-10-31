# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""In-process entry on the in-memory overlay: ``python -m ... --design D --output-root O``.

``onex run-node`` dispatches over a broker to a deployed runtime; this entry runs the same
handler here with the in-memory bus bound, and prints the typed result as JSON.
"""

from __future__ import annotations

import argparse
import sys
import uuid
from pathlib import Path

import yaml

from omnimarket.models.contract_seams_design import ModelContractSeamsDesign
from omnimarket.nodes.node_contract_seams_orchestrator.handlers.handler_contract_seams_run import (
    HandlerContractSeamsRun,
)
from omnimarket.nodes.node_contract_seams_orchestrator.models.model_contract_seams_run import (
    ModelContractSeamsRunRequest,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="node_contract_seams_orchestrator")
    parser.add_argument("--design", required=True, type=Path, help="design YAML file")
    parser.add_argument(
        "--output-root",
        required=True,
        type=Path,
        help="empty directory to scaffold into",
    )
    args = parser.parse_args(argv)
    design = ModelContractSeamsDesign.model_validate(
        yaml.safe_load(args.design.read_text(encoding="utf-8"))
    )
    result = HandlerContractSeamsRun().handle(
        ModelContractSeamsRunRequest(
            correlation_id=uuid.uuid4(),
            design=design,
            output_root=str(args.output_root.resolve()),
        )
    )
    sys.stdout.write(result.model_dump_json(indent=2) + "\n")
    return 0 if result.final_state == "connected" else 1


if __name__ == "__main__":
    raise SystemExit(main())
